import pytest

from bundlesearch.client import Device, PanoramaError
from bundlesearch.collector import (
    METHOD_DIRECT,
    METHOD_PROXY,
    PANORAMA_FILE,
    collect_configs,
    filter_devices,
    parse_filters,
)

from .conftest import FW1_SERIAL, FW1_XML, FW2_SERIAL, FW2_XML, PANORAMA_XML

DEVICES = [
    Device(FW1_SERIAL, "fw-branch-01", "192.0.2.10"),
    Device(FW2_SERIAL, "fw-dc-02", "192.0.2.20"),
    Device("013201003333", "fw-broken", ""),
]
CONFIGS = {FW1_SERIAL: FW1_XML.encode(), FW2_SERIAL: FW2_XML.encode()}


class FakePanorama:
    host = "pano"

    def __init__(self, devices=DEVICES, fail_panorama=False):
        self.devices, self.fail_panorama = devices, fail_panorama
        self.targets: list[str] = []

    def get_connected_devices(self):
        return list(self.devices)

    def clone(self):
        return self

    def get_running_config(self, target=None):
        if target is None:
            if self.fail_panorama:
                raise PanoramaError("pano down")
            return PANORAMA_XML.encode()
        self.targets.append(target)
        if target not in CONFIGS:
            raise PanoramaError("device not connected")
        return CONFIGS[target]


class FakeFirewall:
    created: list[tuple] = []

    def __init__(self, host, verify=True, label=""):
        self.host, self.label = host, label
        FakeFirewall.created.append((host, verify, label))

    def login(self, user, password):
        assert (user, password) == ("fwadmin", "fwpw")

    def get_running_config(self, target=None):
        assert target is None
        serial = {"192.0.2.10": FW1_SERIAL, "192.0.2.20": FW2_SERIAL}[self.host]
        return CONFIGS[serial]


def test_parse_filters():
    assert parse_filters(" fw-* \n\n0132\n") == ["fw-*", "0132"]


def test_filter_devices():
    assert filter_devices(DEVICES, []) == DEVICES
    assert [d.hostname for d in filter_devices(DEVICES, ["FW-BRANCH-*"])] == ["fw-branch-01"]
    assert [d.serial for d in filter_devices(DEVICES, [FW2_SERIAL])] == [FW2_SERIAL]
    assert filter_devices(DEVICES, ["nomatch"]) == []


def test_proxy_collection(tmp_path):
    pano = FakePanorama()
    result = collect_configs(pano, tmp_path, method=METHOD_PROXY, max_workers=2)
    assert (tmp_path / PANORAMA_FILE).read_bytes() == PANORAMA_XML.encode()
    assert (tmp_path / "devices" / FW1_SERIAL / "running-config.xml").exists()
    assert (tmp_path / "devices" / FW2_SERIAL / "running-config.xml").exists()
    assert sorted(pano.targets) == sorted([FW1_SERIAL, FW2_SERIAL, "013201003333"])
    assert result.panorama_collected
    assert len(result.collected) == 2
    assert result.errors == ["fw-broken [013201003333]: device not connected"]
    assert result.serial_map[FW1_SERIAL] == "fw-branch-01"


def test_direct_collection(tmp_path):
    FakeFirewall.created.clear()
    result = collect_configs(
        FakePanorama(), tmp_path,
        method=METHOD_DIRECT,
        include_panorama=False,
        fw_username="fwadmin", fw_password="fwpw",
        client_factory=FakeFirewall, verify=False,
    )
    assert not (tmp_path / PANORAMA_FILE).exists()
    assert sorted(h for h, _, _ in FakeFirewall.created) == ["192.0.2.10", "192.0.2.20"]
    assert all(v is False for _, v, _ in FakeFirewall.created)
    assert len(result.collected) == 2
    assert "management IP" in result.errors[0]


def test_direct_requires_credentials(tmp_path):
    with pytest.raises(ValueError):
        collect_configs(FakePanorama(), tmp_path, method=METHOD_DIRECT)


def test_panorama_failure_recorded(tmp_path):
    result = collect_configs(FakePanorama(fail_panorama=True), tmp_path, patterns=["fw-dc-*"])
    assert not result.panorama_collected
    assert result.errors == ["Panorama: pano down"]
    assert result.collected == ["fw-dc-02 [%s]" % FW2_SERIAL]


def test_device_list_failure_propagates(tmp_path):
    class Broken(FakePanorama):
        def get_connected_devices(self):
            raise PanoramaError("no list")

    with pytest.raises(PanoramaError):
        collect_configs(Broken(), tmp_path)
