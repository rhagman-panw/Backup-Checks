import io
import tarfile

import pytest

from bundlesearch.bundle import BundleError, extract_bundle, iter_files, resolve_device_name

from .conftest import FW1_SERIAL, FW1_XML, FW2_XML, PANORAMA_XML, make_tar


def test_extracts_files(bundle_path, tmp_path):
    root = extract_bundle(bundle_path, tmp_path / "out")
    rels = [rel for _, rel in iter_files(root)]
    assert "running-config.xml" in rels
    assert f"devices/{FW1_SERIAL}/running-config.xml" in rels


def test_extracts_nested(nested_bundle_path, tmp_path):
    root = extract_bundle(nested_bundle_path, tmp_path / "out")
    rels = [rel for _, rel in iter_files(root)]
    assert f"{FW1_SERIAL}.tgz.d/running-config.xml" in rels
    assert f"{FW1_SERIAL}.tgz" not in rels


@pytest.mark.parametrize("name", ["../evil.txt", "/etc/evil", "a/../../evil", "C:/evil.txt"])
def test_rejects_path_traversal(tmp_path, name):
    bad = make_tar(tmp_path / "bad.tgz", {name: b"x"})
    with pytest.raises(BundleError):
        extract_bundle(bad, tmp_path / "out")
    assert not (tmp_path / "evil.txt").exists()


def test_skips_symlinks(tmp_path):
    path = tmp_path / "link.tgz"
    with tarfile.open(path, "w:gz") as tf:
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tf.addfile(info)
        data = b"ok"
        f = tarfile.TarInfo("ok.txt")
        f.size = len(data)
        tf.addfile(f, io.BytesIO(data))
    root = extract_bundle(path, tmp_path / "out")
    rels = [rel for _, rel in iter_files(root)]
    assert rels == ["ok.txt"]


def test_size_cap(tmp_path):
    big = make_tar(tmp_path / "big.tgz", {"a.txt": b"x" * 2000})
    with pytest.raises(BundleError):
        extract_bundle(big, tmp_path / "out", max_total_bytes=1000)


def test_not_an_archive(tmp_path):
    p = tmp_path / "x.tgz"
    p.write_bytes(b"<response status='error'/>")
    with pytest.raises(BundleError):
        extract_bundle(p, tmp_path / "out")


def test_device_names():
    assert resolve_device_name("running-config.xml", PANORAMA_XML.encode()) == "Panorama (pano-lab-01)"
    assert (
        resolve_device_name(f"devices/{FW1_SERIAL}/running-config.xml", FW1_XML.encode())
        == f"fw-branch-01 [{FW1_SERIAL}]"
    )
    assert (
        resolve_device_name(
            f"devices/{FW1_SERIAL}/x.xml", FW1_XML.encode(), {FW1_SERIAL: "fw-from-panorama"}
        )
        == f"fw-from-panorama [{FW1_SERIAL}]"
    )
    assert resolve_device_name("x/fw2.xml", FW2_XML.encode()) == "fw-dc-02"
    assert resolve_device_name("misc/readme.txt", b"hello").startswith("Unidentified")
