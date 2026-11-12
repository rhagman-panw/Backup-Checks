import pytest
import requests

from bundlesearch.client import Device, PanoramaClient, PanoramaError, normalize_host


class FakeResponse:
    def __init__(self, content: bytes = b"", status: int = 200):
        self.content = content
        self.status_code = status


class FakeSession:
    def __init__(self, post=None, exc=None):
        self.verify = True
        self.posts: list[dict] = []
        self._post, self._exc = list(post or []), exc

    def post(self, url, data=None, headers=None, timeout=None):
        self.posts.append({"url": url, "data": data, "headers": headers})
        if self._exc:
            raise self._exc
        return self._post.pop(0)


KEYGEN_OK = b"<response status='success'><result><key>APIKEY123</key></result></response>"
CONNECTED = (
    b"<response status='success'><result><devices>"
    b"<entry name='111'><serial>111</serial><hostname>fw-a</hostname>"
    b"<ip-address>192.0.2.1</ip-address><model>PA-440</model></entry>"
    b"<entry name='222'><serial>222</serial><hostname>fw-b</hostname>"
    b"<ipv6-address>2001:db8::2</ipv6-address></entry>"
    b"</devices></result></response>"
)
CONFIG = (
    b"<response status='success'><result total='1' count='1'>"
    b"<config version='10.2.0'><devices><entry name='localhost.localdomain'>"
    b"<deviceconfig><system><hostname>fw-a</hostname></system></deviceconfig>"
    b"</entry></devices></config></result></response>"
)


def logged_in(*responses):
    s = FakeSession(post=[FakeResponse(KEYGEN_OK), *responses])
    c = PanoramaClient("pano", session=s)
    c.login("admin", "pw")
    return c, s


def test_normalize_host():
    assert normalize_host("https://pano.example.com/") == "pano.example.com"
    assert normalize_host("10.0.0.1:8443") == "10.0.0.1:8443"
    assert normalize_host("[2001:db8::2]") == "[2001:db8::2]"
    with pytest.raises(PanoramaError):
        normalize_host("bad host;rm")


def test_login_uses_post_body_not_url():
    s = FakeSession(post=[FakeResponse(KEYGEN_OK)])
    c = PanoramaClient("pano", session=s)
    c.login("admin", "pw!")
    call = s.posts[0]
    assert call["url"] == "https://pano/api/"
    assert call["data"]["password"] == "pw!"
    assert call["headers"] is None


def test_login_error_does_not_leak_password():
    body = b"<response status='error'><result><msg>Invalid credential pw-secret</msg></result></response>"
    c = PanoramaClient("pano", session=FakeSession(post=[FakeResponse(body, 403)]))
    with pytest.raises(PanoramaError) as ei:
        c.login("admin", "pw-secret")
    assert "pw-secret" not in str(ei.value)
    assert "Login to Panorama failed" in str(ei.value)


def test_connection_error_message_uses_label():
    c = PanoramaClient("192.0.2.1", session=FakeSession(exc=requests.exceptions.SSLError("x")), label="fw-a")
    with pytest.raises(PanoramaError, match="SSL.*fw-a"):
        c.login("a", "b")


def test_connected_devices():
    c, s = logged_in(FakeResponse(CONNECTED))
    devices = c.get_connected_devices()
    assert devices == [
        Device("111", "fw-a", "192.0.2.1", "", "PA-440"),
        Device("222", "fw-b", "", "2001:db8::2", ""),
    ]
    assert devices[1].mgmt_address == "[2001:db8::2]"
    assert "<connected/>" in s.posts[1]["data"]["cmd"]
    assert s.posts[1]["headers"] == {"X-PAN-KEY": "APIKEY123"}


def test_running_config_proxied_with_target():
    c, s = logged_in(FakeResponse(CONFIG))
    xml = c.get_running_config(target="111")
    assert xml.startswith(b"<?xml")
    assert b"<hostname>fw-a</hostname>" in xml
    assert b"\n" in xml.strip()  # pretty-printed
    data = s.posts[1]["data"]
    assert data == {"type": "config", "action": "show", "xpath": "/config", "target": "111"}
    assert "key" not in data


def test_running_config_local_has_no_target():
    c, s = logged_in(FakeResponse(CONFIG))
    c.get_running_config()
    assert "target" not in s.posts[1]["data"]


def test_running_config_error_scrubs_key():
    err = b"<response status='error'><msg><line>device 111 not connected APIKEY123</line></msg></response>"
    c, _ = logged_in(FakeResponse(err))
    with pytest.raises(PanoramaError) as ei:
        c.get_running_config(target="111")
    assert "not connected" in str(ei.value)
    assert "APIKEY123" not in str(ei.value)


def test_clone_shares_key_not_session():
    c, s = logged_in()
    other = c.clone()
    assert other._api_key == "APIKEY123"
    assert other.session is not c.session


def test_requires_login():
    c = PanoramaClient("pano", session=FakeSession())
    with pytest.raises(PanoramaError, match="Not logged in"):
        c.get_connected_devices()
