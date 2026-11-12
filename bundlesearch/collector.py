"""Collect running configs from firewalls listed by Panorama.

Two connection methods:

* ``proxy``  - every request goes to Panorama with ``target=<serial>``; Panorama
               forwards it to the managed firewall. Only Panorama must be reachable.
* ``direct`` - connect to each firewall's management IP (as reported by Panorama)
               with firewall credentials.

Files are written as ``panorama/running-config.xml`` and
``devices/<serial>/running-config.xml`` so the normal search pipeline can be used.
"""
from __future__ import annotations

import fnmatch
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .client import Device, PanoramaError

log = logging.getLogger(__name__)

METHOD_PROXY = "proxy"
METHOD_DIRECT = "direct"
METHODS = (METHOD_PROXY, METHOD_DIRECT)
PANORAMA_FILE = "panorama/running-config.xml"
DEFAULT_WORKERS = 8


@dataclass
class CollectResult:
    devices: list[Device] = field(default_factory=list)  # devices selected for collection
    collected: list[str] = field(default_factory=list)  # display names collected OK
    errors: list[str] = field(default_factory=list)
    panorama_collected: bool = False

    @property
    def serial_map(self) -> dict[str, str]:
        return {d.serial: d.hostname for d in self.devices if d.hostname}


def parse_filters(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def filter_devices(devices: Iterable[Device], patterns: Sequence[str]) -> list[Device]:
    """Keep devices whose hostname or serial matches any glob pattern (case-insensitive)."""
    devices = list(devices)
    if not patterns:
        return devices
    pats = [p.lower() for p in patterns]
    return [
        d for d in devices
        if any(fnmatch.fnmatchcase(v.lower(), p) for v in (d.serial, d.hostname) if v for p in pats)
    ]


def _safe_name(serial: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", serial) or "unknown"


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def collect_configs(
    panorama,
    dest: Path,
    *,
    method: str = METHOD_PROXY,
    patterns: Sequence[str] = (),
    include_panorama: bool = True,
    fw_username: str | None = None,
    fw_password: str | None = None,
    client_factory: Callable | None = None,
    verify: bool = True,
    max_workers: int = DEFAULT_WORKERS,
) -> CollectResult:
    """Collect configs into ``dest``. ``panorama`` must already be logged in.

    Raises ``PanoramaError`` if the device list cannot be read; per-device
    failures are recorded in ``CollectResult.errors``.
    """
    if method not in METHODS:
        raise ValueError(f"Unknown connection method {method!r}.")
    if method == METHOD_DIRECT and (not fw_username or not fw_password or client_factory is None):
        raise ValueError("Firewall credentials are required for direct connections.")

    dest = Path(dest)
    result = CollectResult()
    result.devices = filter_devices(panorama.get_connected_devices(), patterns)

    if include_panorama:
        try:
            _write(dest / PANORAMA_FILE, panorama.get_running_config())
            result.panorama_collected = True
        except PanoramaError as exc:
            result.errors.append(f"Panorama: {exc}")

    def fetch(dev: Device) -> bytes:
        if method == METHOD_PROXY:
            return panorama.clone().get_running_config(target=dev.serial)
        addr = dev.mgmt_address
        if not addr:
            raise PanoramaError("Panorama did not report a management IP address.")
        assert client_factory is not None  # validated above
        fw = client_factory(addr, verify=verify, label=dev.hostname or dev.serial)
        fw.login(fw_username, fw_password)
        return fw.get_running_config()

    def task(dev: Device) -> tuple[Device, bytes | None, str | None]:
        try:
            return dev, fetch(dev), None
        except PanoramaError as exc:
            return dev, None, str(exc)
        except Exception as exc:  # noqa: BLE001 - one bad device must not stop the run
            log.warning("Unexpected error collecting %s: %s", dev.serial, type(exc).__name__)
            return dev, None, f"Unexpected error ({type(exc).__name__})."

    if result.devices:
        workers = max(1, min(max_workers, len(result.devices)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for dev, data, err in pool.map(task, result.devices):
                if err is not None or data is None:
                    result.errors.append(f"{dev.display}: {err}")
                    continue
                _write(dest / "devices" / _safe_name(dev.serial) / "running-config.xml", data)
                result.collected.append(dev.display)
    return result
