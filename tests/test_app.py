import csv
import io
import re
import shutil

import pytest

from app import create_app
from bundlesearch.client import PanoramaError

from .conftest import FW1_SERIAL, FW2_SERIAL


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


def test_index_has_password_field(client):
    html = client.get("/").get_data(as_text=True)
    assert 'type="password"' in html
    assert 'value=""' in html


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
        # term itself is echoed in the summary, but never as a match/context
        assert f"<mark>{secret}" not in html
    assert "SECRETHASHVALUE" not in html
    assert "PanoCommunity" not in html


def test_exports(client, bundle_path):
    html = _upload(client, bundle_path).get_data(as_text=True)
    token = re.search(r"/export/([\w-]+)/csv", html).group(1)

    resp = client.get(f"/export/{token}/csv")
    assert resp.status_code == 200
    assert "attachment" in resp.headers["Content-Disposition"]
    rows = list(csv.reader(io.StringIO(resp.get_data(as_text=True).lstrip("\ufeff"))))
    assert rows[0][:3] == ["Device", "File", "Line"]
    devices = {r[0] for r in rows[1:]}
    assert f"fw-branch-01 [{FW1_SERIAL}]" in devices
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


class FakeClient:
    def __init__(self, host, verify=True, bundle=None, fail=None):
        self.host, self._bundle, self._fail = host, bundle, fail

    def login(self, user, password):
        if self._fail:
            raise PanoramaError(self._fail)

    def get_device_map(self):
        return {FW1_SERIAL: "fw-branch-from-pano"}

    def export_bundle(self, dest):
        shutil.copy(self._bundle, dest)


def test_panorama_mode(bundle_path):
    app = create_app({
        "TESTING": True,
        "CLIENT_FACTORY": lambda host, verify: FakeClient(host, verify, bundle=bundle_path),
    })
    resp = app.test_client().post("/search", data={
        "mode": "panorama", "host": "pano", "username": "admin",
        "password": "SuperSecretPw", "terms": "10.1.1.10", "context": "1",
    })
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert f"fw-branch-from-pano [{FW1_SERIAL}]" in html
    assert "SuperSecretPw" not in html


def test_panorama_error_does_not_echo_password():
    app = create_app({
        "TESTING": True,
        "CLIENT_FACTORY": lambda host, verify: FakeClient(host, verify, fail="Login failed."),
    })
    resp = app.test_client().post("/search", data={
        "mode": "panorama", "host": "pano", "username": "admin",
        "password": "SuperSecretPw", "terms": "x",
    })
    html = resp.get_data(as_text=True)
    assert resp.status_code == 502
    assert "Login failed." in html
    assert "SuperSecretPw" not in html
    assert 'value="admin"' in html
