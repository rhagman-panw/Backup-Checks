from bundlesearch.redact import MASK, redact

from .conftest import FW1_XML, PANORAMA_XML


def test_redacts_password_elements():
    out = redact("<password>hunter2</password>")
    assert out == f"<password>{MASK}</password>"


def test_redacts_suffix_named_elements():
    for tag in ("bind-password", "authpwd", "privpwd", "shared-secret", "passphrase", "phash"):
        out = redact(f"<{tag}>s3cr3t</{tag}>")
        assert "s3cr3t" not in out, tag
        assert MASK in out


def test_redacts_nested_psk_key_and_snmp():
    out = redact(FW1_XML) + redact(PANORAMA_XML)
    for secret in ("TopSecretPSK123", "FW1HASHVALUE", "LdapBindPw!", "SECRETHASHVALUE", "PanoCommunity"):
        assert secret not in out


def test_redacts_attributes():
    out = redact('<entry name="x" password="abc123" secret=\'zz\'/>')
    assert "abc123" not in out and "zz" not in out
    assert 'name="x"' in out


def test_keeps_non_secret_content_and_line_count():
    text = "<a>\n  <hostname>fw1</hostname>\n  <password>\n  multi\n  </password>\n</a>"
    out = redact(text)
    assert "<hostname>fw1</hostname>" in out
    assert "multi" not in out
    assert out.count("\n") == text.count("\n")


def test_does_not_touch_elements_with_children():
    text = "<pre-shared-key>\n  <key>abc</key>\n</pre-shared-key>"
    out = redact(text)
    assert "<pre-shared-key>" in out and "abc" not in out


def test_non_secret_names_with_key_substring_untouched():
    text = "<keepalive>yes</keepalive><monkey>banana</monkey>"
    assert redact(text) == text
