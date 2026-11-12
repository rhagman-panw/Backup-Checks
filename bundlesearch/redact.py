"""Mask secret values in PAN-OS XML configuration text.

Redaction happens on the full file text *before* it is split into lines and
searched, so secrets never reach the UI, the exports, or search matches.
The number of lines is preserved so line numbers stay correct.
"""
from __future__ import annotations

import re

MASK = "********"

# Exact element names that hold secrets.
_EXACT_TAGS = (
    "key",
    "private-key",
    "auth-key",
    "api-key",
    "pre-shared-key",
    "preshared-key",
    "shared-key",
    "secret-key",
    "access-key",
    "md5-key",
    "encryption-key",
    "community",
    "phash",
)

# Element / attribute names ending in one of these suffixes are treated as secret
# (e.g. password, bind-password, auth-password, shared-secret, authpwd, privpwd).
_SUFFIXES = (
    "password", "passwd", "passphrase", "secret", "pwd", "psk", "phash",
    "community", "community-string",
)

_suffix_alt = "|".join(_SUFFIXES)
_exact_alt = "|".join(re.escape(t) for t in sorted(_EXACT_TAGS, key=len, reverse=True))
_TAG = rf"(?:[\w.-]*?(?:{_suffix_alt})|{_exact_alt})"

# <tag attr="...">value</tag> where value contains no child elements (may span lines).
_ELEMENT_RE = re.compile(
    rf"(?P<open><(?P<tag>{_TAG})(?:\s[^<>]*?)?(?<!/)>)(?P<val>[^<]+)(?P<close></(?P=tag)\s*>)",
    re.IGNORECASE,
)

# name="value" / name='value' attributes.
_ATTR_RE = re.compile(
    rf"(?P<name>\b[\w.-]*(?:{_suffix_alt}))(?P<eq>\s*=\s*)(?P<q>[\"'])(?P<val>.*?)(?P=q)",
    re.IGNORECASE,
)

# Non-whitespace content on a line (keeps indentation and newlines intact).
_LINE_CONTENT_RE = re.compile(r"\S[^\r\n]*")


def _mask_value(value: str) -> str:
    return _LINE_CONTENT_RE.sub(MASK, value)


def _element_sub(m: re.Match) -> str:
    return m.group("open") + _mask_value(m.group("val")) + m.group("close")


def _attr_sub(m: re.Match) -> str:
    val = MASK if m.group("val") else ""
    return f"{m.group('name')}{m.group('eq')}{m.group('q')}{val}{m.group('q')}"


def redact(text: str) -> str:
    """Return ``text`` with secret element and attribute values replaced by ``MASK``."""
    text = _ELEMENT_RE.sub(_element_sub, text)
    return _ATTR_RE.sub(_attr_sub, text)
