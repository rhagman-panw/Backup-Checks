import csv
import io
import re
import shutil
from pathlib import Path

import pytest

from app import create_app
from bundlesearch.client import Device, PanoramaError
from bundlesearch.remote import RemoteError, RemoteFile

from .conftest import FW1_SERIAL, FW1_XML, FW2_SERIAL, FW2_XML, PANORAMA_XML


@pytest.fixture
def app():
    return create_app({"TESTING": True})


@pytest.fixture
def client(app):
    return app.test_client()


def _upload(client, bundle_path, terms="10.1.1.10", **extra):
    data = {"mode": "upload", "terms": terms, "context": "2", **extra}
    with open(bundle_path, "rb") as fh:
        data["bundle"] = (fh, "bundle.tgz")
        return client.post("/search", data=data, content_type="multipart/form-data")


def test_index_has_all_modes_and_password_fields(client):
    html = client.get("/").get_data(as_text=True)
    for mode in ("panorama", "linux", "upload"):
        assert f'value="{mode}"' in html
    for name in ("password", "fw_password", "ssh_password", "ssh_key_passphrase"):
        assert re.search(rf'type="password" name="{name}" value=""', html), name
    assert 'value="proxy" checked' in html


def test_upload_search_shows_devices_and_context(client, bundle_path):
    resp = _upload(client, bundle_path)
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Panorama (pano-lab-01)" in html
    assert f"fw-branch-01 [{FW1_SERIAL}]" in html
    assert f"fw-dc-02 [{FW2_SERIAL}]" in html
    assert "<mark>10.1.1.10</mark>" in html
    assert "web-server-01" in html  # context line above the match
    assert "binary.bin" not in html


def test_secrets_never_rendered_or_matched(client, bundle_path):
    resp = _upload(client, bundle_path, terms="TopSecretPSK123\nLdapBindPw\nhostname")
    html = resp.get_data(as_text=True)
    for secret in ("TopSecretPSK123", "LdapBindPw", "SECRETHASHVALUE", "FW1HASHVALUE", "PanoCommunity"):
        assert f"<mark>{secret}" not in html
    assert "SECRETHASHVALUE" not in html
    assert "PanoCommunity" not in html


def test_exports(client, bundle_path):
    html = _upload(client, bundle_path).get_data(as_text=True)
    token = re.search(r"/export/([\w-]+)/csv", html).group(1)  # type: ignore[union-attr]

    resp = client.get(f"/export/{token}/csv")
    assert resp.status_code == 200
    assert "attachment" in resp.headers["Content-Disposition"]
    rows = list(csv.reader(io.StringIO(resp.get_data(as_text=True).lstrip("\ufeff"))))
    assert rows[0][:3] == ["Device", "File", "Line"]
    assert f"fw-branch-01 [{FW1_SERIAL}]" in {r[0] for r in rows[1:]}
    assert all("10.1.1.10" in r[4] for r in rows[1:])

    txt = client.get(f"/export/{token}/txt").get_data(as_text=True)
    assert "Device: Panorama (pano-lab-01)" in txt
    assert "TopSecretPSK123" not in txt

    assert client.get(f"/export/{token}/pdf").status_code == 404
    assert client.get("/export/nope/csv").status_code == 302


def test_validation_errors(client, bundle_path):
    resp = _upload(client, bundle_path, terms="")
    assert resp.status_code == 400
    assert "at least one" in resp.get_data(as_text=True)
    resp = client.post("/search", data={"mode": "panorama", "terms": "x", "host": "pano"})
    assert resp.status_code == 400


# ------------------------------------------------------------ Panorama mode
CONFIGS = {FW1_SERIAL: FW1_XML.encode(), FW2_SERIAL: FW2_XML.encode()}


class FakeClient:
    instances: list["FakeClient"] = []

    def __init__(self, host, verify=True, label="Panorama", fail=None):
        self.host, self.verify, self.label, self._fail = host, verify, label, fail
        self.logins: list[tuple] = []
        self.targets: list = []
        FakeClient.instances.append(self)

    def login(self, user, password):
        if self._fail:
            raise PanoramaError(self._fail)
        self.logins.append((user, password))

    def clone(self):
        return self

    def get_connected_devices(self):
        return [
            Device(FW1_SERIAL, "fw-branch-from-pano", "192.0.2.10"),
            Device(FW2_SERIAL, "fw-dc-02", "192.0.2.20"),
        ]

    def get_running_config(self, target=None):
        self.targets.append(target)
        if self.label != "Panorama":  # direct firewall connection
            return CONFIGS[{"192.0.2.10": FW1_SERIAL, "192.0.2.20": FW2_SERIAL}[self.host]]
        if target is None:
            return PANORAMA_XML.encode()
        if target == FW2_SERIAL:
            raise PanoramaError("device not connected")
        return CONFIGS[target]


PANO_FORM = {
    "mode": "panorama", "host": "pano", "username": "admin",
    "password": "SuperSecretPw", "terms": "10.1.1.10", "context": "1",
    "include_panorama": "on", "verify_ssl": "on",
}


def _pano_app(**factory_kw):
    FakeClient.instances.clear()
    return create_app({
        "TESTING": True,
        "CLIENT_FACTORY": lambda host, verify=True, label="Panorama": FakeClient(host, verify, label, **factory_kw),
    })


def test_panorama_proxy_mode():
    resp = _pano_app().test_client().post("/search", data={**PANO_FORM, "method": "proxy"})
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Panorama (pano-lab-01)" in html
    assert f"fw-branch-from-pano [{FW1_SERIAL}]" in html
    assert "1 device(s) could not be collected" in html
    assert "device not connected" in html
    assert "via Panorama proxy" in html
    assert "SuperSecretPw" not in html
    assert len(FakeClient.instances) == 1  # everything goes through Panorama
    assert set(FakeClient.instances[0].targets) == {None, FW1_SERIAL, FW2_SERIAL}


def test_panorama_direct_mode_uses_firewall_credentials():
    form = {**PANO_FORM, "method": "direct", "fw_username": "fwadmin", "fw_password": "FwPw!",
            "include_panorama": ""}
    resp = _pano_app().test_client().post("/search", data=form)
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert f"fw-dc-02 [{FW2_SERIAL}]" in html
    assert "direct to firewalls" in html
    firewalls = [c for c in FakeClient.instances if c.label != "Panorama"]
    assert sorted(c.host for c in firewalls) == ["192.0.2.10", "192.0.2.20"]
    assert all(c.logins == [("fwadmin", "FwPw!")] for c in firewalls)
    assert "FwPw!" not in html


def test_panorama_device_filter_no_match():
    form = {**PANO_FORM, "device_filter": "nothing-*", "include_panorama": ""}
    resp = _pano_app().test_client().post("/search", data=form)
    assert resp.status_code == 400
    assert "No connected devices matched" in resp.get_data(as_text=True)


def test_panorama_error_does_not_echo_password():
    resp = _pano_app(fail="Login to Panorama failed.").test_client().post("/search", data=PANO_FORM)
    html = resp.get_data(as_text=True)
    assert resp.status_code == 502
    assert "Login to Panorama failed." in html
    assert "SuperSecretPw" not in html
    assert 'value="admin"' in html


# --------------------------------------------------------------- Linux mode
class FakeFetcher:
    last: "FakeFetcher | None" = None

    def __init__(self, host, *, port, username, password, pkey, accept_unknown_host, transfer,
                 bundle: Path | None = None, fail: str | None = None):
        self.host, self.kw = host, dict(port=port, username=username, password=password, pkey=pkey,
                                        accept_unknown_host=accept_unknown_host, transfer=transfer)
        self._bundle, self._fail = bundle, fail
        FakeFetcher.last = self

    def fetch_latest(self, remote_dir, pattern, dest, *, max_bytes):
        if self._fail:
            raise RemoteError(self._fail)
        assert self._bundle is not None
        shutil.copy(self._bundle, dest)
        return RemoteFile(f"{remote_dir}/latest.tgz", 1)


LINUX_FORM = {
    "mode": "linux", "ssh_host": "backup01", "ssh_port": "2222", "ssh_username": "svc",
    "ssh_password": "SshSecret", "remote_dir": "/srv/bundles", "remote_pattern": "*.tgz",
    "transfer": "scp", "terms": "10.1.1.10", "context": "1",
}


def _linux_app(**kw):
    return create_app({
        "TESTING": True,
        "REMOTE_FACTORY": lambda host, **a: FakeFetcher(host, **a, **kw),
    })


def test_linux_mode(bundle_path):
    resp = _linux_app(bundle=bundle_path).test_client().post("/search", data=LINUX_FORM)
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Linux server backup01: /srv/bundles/latest.tgz" in html
    assert f"fw-branch-01 [{FW1_SERIAL}]" in html
    assert "SshSecret" not in html
    assert FakeFetcher.last is not None
    assert FakeFetcher.last.kw["transfer"] == "scp"
    assert FakeFetcher.last.kw["port"] == "2222"
    assert FakeFetcher.last.kw["password"] == "SshSecret"


def test_linux_mode_error_hides_password():
    resp = _linux_app(fail="SSH authentication failed for svc@backup01.").test_client().post(
        "/search", data=LINUX_FORM
    )
    html = resp.get_data(as_text=True)
    assert resp.status_code == 502
    assert "SSH authentication failed" in html
    assert "SshSecret" not in html
    assert 'value="/srv/bundles"' in html


def test_linux_mode_bad_key():
    data = {**LINUX_FORM, "ssh_key": (io.BytesIO(b"garbage"), "id_rsa")}
    resp = _linux_app().test_client().post("/search", data=data, content_type="multipart/form-data")
    assert resp.status_code == 502
    assert "private key" in resp.get_data(as_text=True)
