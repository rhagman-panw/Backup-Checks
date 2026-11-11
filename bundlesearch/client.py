"""Minimal PAN-OS XML API client for Panorama.

Credentials are sent only in a POST body (keygen) and the API key only in the
``X-PAN-KEY`` header, so neither appears in URLs or access logs. Nothing is
persisted. Error messages never include credentials.
"""
from __future__ import annotations

import re
import tarfile
from pathlib import Path
from xml.etree.ElementTree import ParseError

import defusedxml.ElementTree as DET
import requests
import urllib3
from defusedxml import DefusedXmlException

# Export category for "Export Panorama and devices config bundle".
# Check this against your PAN-OS version (API browser: https://<panorama>/api).
EXPORT_CATEGORY = "device-config-bundle"
SHOW_DEVICES_CMD = "<show><devices><all/></devices></show>"

_HOST_RE = re.compile(r"^(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(?::\d{1,5})?$")
_MASK = "********"


class PanoramaError(Exception):
    """User-presentable error (never contains credentials)."""


def normalize_host(host: str) -> str:
    h = (host or "").strip()
    h = re.sub(r"^https?://", "", h, flags=re.IGNORECASE).split("/", 1)[0]
    if not h or not _HOST_RE.match(h):
        raise PanoramaError("Enter a valid Panorama hostname or IP address.")
    return h


class PanoramaClient:
    def __init__(
        self,
        host: str,
        *,
        verify: bool = True,
        timeout: float = 30,
        export_timeout: float = 1800,
        session: requests.Session | None = None,
    ) -> None:
        self.host = normalize_host(host)
        self.base_url = f"https://{self.host}/api/"
        self.timeout = timeout
        self.export_timeout = export_timeout
        self.session = session or requests.Session()
        self.session.verify = verify
        if not verify:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self._api_key: str | None = None

    # ------------------------------------------------------------------ helpers
    def _scrub(self, text: str, *extra: str) -> str:
        for secret in (self._api_key, *extra):
            if secret:
                text = text.replace(secret, _MASK)
        return text

    def _connection_message(self, exc: Exception) -> str:
        if isinstance(exc, requests.exceptions.SSLError):
            return (
                f"SSL certificate verification failed for {self.host}. Fix the certificate "
                "or untick 'Verify SSL certificate' (lab / self-signed only)."
            )
        if isinstance(exc, requests.exceptions.Timeout):
            return f"Timed out talking to Panorama at {self.host}."
        if isinstance(exc, requests.exceptions.ConnectionError):
            return f"Could not connect to Panorama at {self.host}."
        return f"Request to Panorama failed ({type(exc).__name__})."

    @staticmethod
    def _error_text(root) -> str:
        el = root.find(".//msg")
        if el is None:
            el = root.find(".//line")
        if el is None:
            return ""
        return " ".join(t.strip() for t in el.itertext() if t.strip())

    def _parse_ok(self, resp, *secrets: str):
        try:
            root = DET.fromstring(resp.content)
        except (ParseError, DefusedXmlException, ValueError):
            raise PanoramaError(
                f"Unexpected response from Panorama (HTTP {resp.status_code})."
            ) from None
        if root.get("status") != "success":
            detail = self._error_text(root) or f"HTTP {resp.status_code}"
            raise PanoramaError(self._scrub(f"Panorama returned an error: {detail}", *secrets))
        return root

    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            raise PanoramaError("Not logged in to Panorama.")
        return {"X-PAN-KEY": self._api_key}

    # --------------------------------------------------------------------- API
    def login(self, username: str, password: str) -> None:
        """Generate an API key (kept in memory only)."""
        try:
            resp = self.session.post(
                self.base_url,
                data={"type": "keygen", "user": username, "password": password},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise PanoramaError(self._connection_message(exc)) from None
        try:
            root = self._parse_ok(resp, password)
        except PanoramaError as exc:
            raise PanoramaError(f"Login failed. {exc}") from None
        key = (root.findtext("./result/key") or "").strip()
        if not key:
            raise PanoramaError("Login failed: Panorama did not return an API key.")
        self._api_key = key

    def get_device_map(self) -> dict[str, str]:
        """Return ``{serial: hostname}`` for all managed devices."""
        try:
            resp = self.session.post(
                self.base_url,
                data={"type": "op", "cmd": SHOW_DEVICES_CMD},
                headers=self._headers(),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise PanoramaError(self._connection_message(exc)) from None
        root = self._parse_ok(resp)
        mapping: dict[str, str] = {}
        for entry in root.iterfind(".//devices/entry"):
            serial = (entry.findtext("serial") or entry.get("name") or "").strip()
            hostname = (entry.findtext("hostname") or "").strip()
            if serial and hostname:
                mapping[serial] = hostname
        return mapping

    def export_bundle(self, dest: Path, *, chunk_size: int = 1024 * 1024) -> int:
        """Stream the Panorama + devices config bundle to ``dest``; return bytes written."""
        dest = Path(dest)
        params = {"type": "export", "category": EXPORT_CATEGORY}
        written = 0
        try:
            with self.session.get(
                self.base_url,
                params=params,
                headers=self._headers(),
                stream=True,
                timeout=(self.timeout, self.export_timeout),
            ) as resp:
                status = resp.status_code
                with open(dest, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=chunk_size):
                        if chunk:
                            fh.write(chunk)
                            written += len(chunk)
        except requests.RequestException as exc:
            raise PanoramaError(self._connection_message(exc)) from None

        if status == 200 and written and tarfile.is_tarfile(dest):
            return written

        with open(dest, "rb") as fh:
            head = fh.read(65536)
        detail = ""
        try:
            root = DET.fromstring(head)
            detail = self._error_text(root)
        except (ParseError, DefusedXmlException, ValueError):
            pass
        msg = f"Panorama did not return a config bundle (HTTP {status})"
        raise PanoramaError(self._scrub(f"{msg}: {detail}" if detail else msg + "."))
