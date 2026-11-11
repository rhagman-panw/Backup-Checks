from pathlib import Path

import pytest
import requests

from bundlesearch.client import EXPORT_CATEGORY, PanoramaClient, PanoramaError, normalize_host

from .conftest import FW1_XML, make_tar


class FakeResponse:
    def __init__(self, content: bytes = b"", status: int = 200):
        self.content = content
        self.status_code = status

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i : i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeSession:
    def __init__(self, post=None, get=None, exc=None):
        self.verify = True
        self.posts: list[dict] = []
        self.gets: list[dict] = []
        self._post, self._get, self._exc = post or [], get, exc

    def post(self, url, data=None, headers=None, timeout=None):
        self.posts.append({"url": url, "data": data, "headers": headers})
        if self._exc:
            raise self._exc
        return self._post.pop(0)

    def get(self, url, params=None, headers=None, stream=False, timeout=None):
        self.gets.append({"url": url, "params": params, "headers": headers})
        return self._get


KEYGEN_OK = b"<response status='success'><result><key>APIKEY123</key></result></response>"


def test_normalize_host():
    assert normalize_host("https://pano.example.com/") == "pano.example.com"
    assert normalize_host("10.0.0.1:8443") == "10.0.0.1:8443"
    with pytest.raises(PanoramaError):
        normalize_host("bad host;rm")


def test_login_uses_post_body_not_url():
    s = FakeSession(post=[FakeResponse(KEYGEN_OK)])
    c = PanoramaClient("pano", session=s)
    c.login("admin", "pw!")
    call = s.posts[0]
    assert call["url"] == "https://pano/api/"
    assert call["data"]["password"] == "pw!"
    assert "pw!" not in call["url"]


def test_login_error_does_not_leak_password():
    body = b"<response status='error'><result><msg>Invalid credential pw-secret</msg></result></response>"
    s = FakeSession(post=[FakeResponse(body, 403)])
    c = PanoramaClient("pano", session=s)
    with pytest.raises(PanoramaError) as ei:
        c.login("admin", "pw-secret")
    assert "pw-secret" not in str(ei.value)
    assert "Login failed" in str(ei.value)


def test_connection_error_message():
    s = FakeSession(exc=requests.exceptions.SSLError("x"))
    c = PanoramaClient("pano", session=s)
    with pytest.raises(PanoramaError, match="SSL"):
        c.login("a", "b")


def test_device_map():
    devices = (
        b"<response status='success'><result><devices>"
        b"<entry name='111'><serial>111</serial><hostname>fw-a</hostname></entry>"
        b"<entry name='222'><serial>222</serial><hostname>fw-b</hostname></entry>"
        b"</devices></result></response>"
    )
    s = FakeSession(post=[FakeResponse(KEYGEN_OK), FakeResponse(devices)])
    c = PanoramaClient("pano", session=s)
    c.login("a", "b")
    assert c.get_device_map() == {"111": "fw-a", "222": "fw-b"}
    assert s.posts[1]["headers"] == {"X-PAN-KEY": "APIKEY123"}


def test_export_bundle_streams_to_file(tmp_path: Path):
    tgz = make_tar(tmp_path / "src.tgz", {"running-config.xml": FW1_XML.encode()}).read_bytes()
    s = FakeSession(post=[FakeResponse(KEYGEN_OK)], get=FakeResponse(tgz))
    c = PanoramaClient("pano", session=s)
    c.login("a", "b")
    dest = tmp_path / "out.tgz"
    assert c.export_bundle(dest, chunk_size=64) == len(tgz)
    assert dest.read_bytes() == tgz
    assert s.gets[0]["params"] == {"type": "export", "category": EXPORT_CATEGORY}
    assert "key" not in s.gets[0]["params"]
    assert s.gets[0]["headers"] == {"X-PAN-KEY": "APIKEY123"}


def test_export_bundle_error_response(tmp_path: Path):
    err = b"<response status='error'><msg><line>export failed APIKEY123</line></msg></response>"
    s = FakeSession(post=[FakeResponse(KEYGEN_OK)], get=FakeResponse(err))
    c = PanoramaClient("pano", session=s)
    c.login("a", "b")
    with pytest.raises(PanoramaError) as ei:
        c.export_bundle(tmp_path / "out.tgz")
    assert "export failed" in str(ei.value)
    assert "APIKEY123" not in str(ei.value)


def test_requires_login(tmp_path: Path):
    c = PanoramaClient("pano", session=FakeSession())
    with pytest.raises(PanoramaError):
        c.export_bundle(tmp_path / "x.tgz")
