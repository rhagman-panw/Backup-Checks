"""Minimal PAN-OS XML API client for Panorama and firewalls.

Credentials are sent only in a POST body (keygen) and the API key only in the
``X-PAN-KEY`` header, so neither appears in URLs or access logs. Nothing is
persisted. Error messages never include credentials or API keys.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from xml.etree.ElementTree import ParseError

import defusedxml.ElementTree as DET
import requests
import urllib3
from defusedxml import DefusedXmlException

SHOW_CONNECTED_CMD = "<show><devices><connected/></devices></show>"
RUNNING_CONFIG_XPATH = "/config"

_HOST_RE = re.compile(r"^(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(?::\d{1,5})?$")
_MASK = "********"


class PanoramaError(Exception):
    """User-presentable error (never contains credentials)."""


@dataclass(frozen=True)
class Device:
    """A firewall managed by Panorama."""

    serial: str
    hostname: str = ""
    ip: str = ""
    ipv6: str = ""
    model: str = ""

    @property
    def display(self) -> str:
        return f"{self.hostname or 'unknown'} [{self.serial}]"

    @property
    def mgmt_address(self) -> str:
        if self.ip:
            return self.ip
        return f"[{self.ipv6}]" if self.ipv6 else ""


def normalize_host(host: str) -> str:
    h = (host or "").strip()
    h = re.sub(r"^https?://", "", h, flags=re.IGNORECASE).split("/", 1)[0]
    if not h or not _HOST_RE.match(h):
        raise PanoramaError("Enter a valid hostname or IP address.")
    return h


class PanoramaClient:
    """PAN-OS XML API client. Works for Panorama and (with ``label``) for firewalls."""

    def __init__(
        self,
        host: str,
        *,
        verify: bool = True,
        timeout: float = 30,
        config_timeout: float = 600,
        session: requests.Session | None = None,
        label: str = "Panorama",
    ) -> None:
        self.host = normalize_host(host)
        self.label = label
        self.base_url = f"https://{self.host}/api/"
        self.verify = verify
        self.timeout = timeout
        self.config_timeout = config_timeout
        self.session = session or requests.Session()
        self.session.verify = verify
        if not verify:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self._api_key: str | None = None

    def clone(self) -> "PanoramaClient":
        """Copy that shares the API key but has its own HTTP session (for use in threads)."""
        other = PanoramaClient(
            self.host,
            verify=self.verify,
            timeout=self.timeout,
            config_timeout=self.config_timeout,
            label=self.label,
        )
        other._api_key = self._api_key
        return other

    # ------------------------------------------------------------------ helpers
    def _scrub(self, text: str, *extra: str) -> str:
        for secret in (self._api_key, *extra):
            if secret:
                text = text.replace(secret, _MASK)
        return text

    def _connection_message(self, exc: Exception) -> str:
        where = f"{self.label} at {self.host}" if self.label != self.host else self.host
        if isinstance(exc, requests.exceptions.SSLError):
            return (
                f"SSL certificate verification failed for {where}. Fix the certificate "
                "or untick 'Verify SSL certificate' (lab / self-signed only)."
            )
        if isinstance(exc, requests.exceptions.Timeout):
            return f"Timed out talking to {where}."
        if isinstance(exc, requests.exceptions.ConnectionError):
            return f"Could not connect to {where}."
        return f"Request to {where} failed ({type(exc).__name__})."

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
                f"Unexpected response from {self.label} (HTTP {resp.status_code})."
            ) from None
        if root.get("status") != "success":
            detail = self._error_text(root) or f"HTTP {resp.status_code}"
            raise PanoramaError(self._scrub(f"{self.label} returned an error: {detail}", *secrets))
        return root

    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            raise PanoramaError(f"Not logged in to {self.label}.")
        return {"X-PAN-KEY": self._api_key}

    def _post(self, data: dict, *, auth: bool = True, timeout=None):
        headers = self._headers() if auth else None
        try:
            return self.session.post(
                self.base_url, data=data, headers=headers, timeout=timeout or self.timeout
            )
        except requests.RequestException as exc:
            raise PanoramaError(self._connection_message(exc)) from None

    # --------------------------------------------------------------------- API
    def login(self, username: str, password: str) -> None:
        """Generate an API key (kept in memory only)."""
        resp = self._post(
            {"type": "keygen", "user": username, "password": password}, auth=False
        )
        try:
            root = self._parse_ok(resp, password)
        except PanoramaError as exc:
            raise PanoramaError(f"Login to {self.label} failed. {exc}") from None
        key = (root.findtext("./result/key") or "").strip()
        if not key:
            raise PanoramaError(f"Login to {self.label} failed: no API key returned.")
        self._api_key = key

    def get_connected_devices(self) -> list[Device]:
        """Return the firewalls currently connected to Panorama."""
        root = self._parse_ok(self._post({"type": "op", "cmd": SHOW_CONNECTED_CMD}))
        devices: list[Device] = []
        for entry in root.iterfind(".//devices/entry"):
            serial = (entry.findtext("serial") or entry.get("name") or "").strip()
            if not serial:
                continue
            devices.append(
                Device(
                    serial=serial,
                    hostname=(entry.findtext("hostname") or "").strip(),
                    ip=(entry.findtext("ip-address") or "").strip(),
                    ipv6=(entry.findtext("ipv6-address") or "").strip(),
                    model=(entry.findtext("model") or "").strip(),
                )
            )
        return devices

    def get_running_config(self, target: str | None = None) -> bytes:
        """Return the running configuration as XML.

        With ``target`` set to a firewall serial, Panorama proxies the request to
        that managed firewall.
        """
        data = {"type": "config", "action": "show", "xpath": RUNNING_CONFIG_XPATH}
        if target:
            data["target"] = target
        root = self._parse_ok(self._post(data, timeout=(self.timeout, self.config_timeout)))
        cfg = root.find("./result/config")
        if cfg is None:
            raise PanoramaError(f"{self.label} returned no running configuration.")
        ET.indent(cfg)
        return b'<?xml version="1.0"?>\n' + ET.tostring(cfg, encoding="utf-8") + b"\n"
