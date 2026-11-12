"""Safe extraction of config bundles and device name resolution."""
from __future__ import annotations

import logging
import re
import shutil
import tarfile
from pathlib import Path, PurePosixPath
from typing import Iterator, Mapping
from xml.etree.ElementTree import ParseError

import defusedxml.ElementTree as DET
from defusedxml import DefusedXmlException

log = logging.getLogger(__name__)

DEFAULT_MAX_TOTAL_BYTES = 4 * 1024**3  # 4 GiB extracted
DEFAULT_MAX_MEMBERS = 50_000
MAX_NESTING = 3
NESTED_SUFFIXES = (".tgz", ".tar.gz", ".tar")
NESTED_DIR_SUFFIX = ".d"

SERIAL_RE = re.compile(r"(?<!\d)(\d{12,15})(?!\d)")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class BundleError(Exception):
    """Raised for unreadable or unsafe archives."""


class _Budget:
    def __init__(self, max_bytes: int, max_members: int) -> None:
        self.max_bytes, self.max_members = max_bytes, max_members
        self.bytes = self.members = 0

    def add_member(self) -> None:
        self.members += 1
        if self.members > self.max_members:
            raise BundleError(f"Archive has too many entries (limit {self.max_members}).")

    def add_bytes(self, n: int) -> None:
        self.bytes += n
        if self.bytes > self.max_bytes:
            raise BundleError(
                f"Archive expands beyond the size limit ({self.max_bytes // 1024**2} MiB)."
            )


def _safe_target(dest: Path, name: str) -> Path:
    normalized = name.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if normalized.startswith("/") or _DRIVE_RE.match(normalized) or ".." in pure.parts:
        raise BundleError(f"Unsafe path in archive: {name!r}")
    parts = [p for p in pure.parts if p not in ("", ".")]
    target = dest.joinpath(*parts).resolve() if parts else dest
    if target != dest and dest not in target.parents:
        raise BundleError(f"Unsafe path in archive: {name!r}")
    return target


def _safe_extract(archive: Path, dest: Path, budget: _Budget) -> None:
    if not tarfile.is_tarfile(archive):
        raise BundleError(f"{archive.name} is not a valid .tgz / .tar archive.")
    dest.mkdir(parents=True, exist_ok=True)
    dest = dest.resolve()
    try:
        with tarfile.open(archive, "r:*") as tf:
            for member in tf:
                budget.add_member()
                if not (member.isfile() or member.isdir()):
                    log.warning("Skipping non-regular archive entry %r", member.name)
                    continue
                target = _safe_target(dest, member.name)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                budget.add_bytes(member.size)
                target.parent.mkdir(parents=True, exist_ok=True)
                src = tf.extractfile(member)
                if src is None:
                    continue
                with src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out, 1024 * 1024)
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise BundleError(f"Could not read archive {archive.name}: {exc}") from None


def _extract_nested(root: Path, budget: _Budget, depth: int) -> None:
    if depth > MAX_NESTING:
        return
    candidates = [
        p for p in sorted(root.rglob("*"))
        if p.is_file() and p.name.lower().endswith(NESTED_SUFFIXES)
    ]
    for path in candidates:
        if not tarfile.is_tarfile(path):
            continue
        out = path.with_name(path.name + NESTED_DIR_SUFFIX)
        _safe_extract(path, out, budget)
        path.unlink()
        _extract_nested(out, budget, depth + 1)


def extract_bundle(
    archive: Path,
    dest: Path,
    *,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_members: int = DEFAULT_MAX_MEMBERS,
) -> Path:
    """Safely extract ``archive`` (and nested tgz/tar archives) into ``dest``."""
    budget = _Budget(max_total_bytes, max_members)
    _safe_extract(Path(archive), Path(dest), budget)
    _extract_nested(Path(dest), budget, depth=1)
    return Path(dest)


def iter_files(root: Path) -> Iterator[tuple[Path, str]]:
    """Yield ``(path, relative_posix_path)`` for every file under ``root`` in sorted order."""
    root = Path(root)
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield path, path.relative_to(root).as_posix()


def is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def config_identity(data: bytes) -> tuple[str | None, bool]:
    """Return ``(hostname, is_panorama)`` parsed from a PAN-OS XML config, or ``(None, False)``."""
    if not data.lstrip()[:1] == b"<":
        return None, False
    try:
        root = DET.fromstring(data)
    except (ParseError, DefusedXmlException, ValueError):
        return None, False
    el = root.find("./devices/entry/deviceconfig/system/hostname")
    if el is None:
        el = root.find(".//deviceconfig/system/hostname")
    hostname = el.text.strip() if el is not None and el.text and el.text.strip() else None
    is_panorama = any(
        root.find(p) is not None
        for p in ("./panorama", "./devices/entry/device-group", "./devices/entry/template")
    )
    return hostname, is_panorama


def resolve_device_name(
    rel_path: str, data: bytes, serial_map: Mapping[str, str] | None = None
) -> str:
    """Pick the best display name for the device a bundle file belongs to."""
    hostname, is_panorama = config_identity(data)
    if is_panorama:
        return f"Panorama ({hostname})" if hostname else "Panorama"
    serial = None
    if serial_map:
        serial = next((p for p in PurePosixPath(rel_path).parts if p in serial_map), None)
    if serial is None:
        m = SERIAL_RE.search(rel_path)
        serial = m.group(1) if m else None
    if serial and serial_map and serial_map.get(serial):
        return f"{serial_map[serial]} [{serial}]"
    if hostname:
        return f"{hostname} [{serial}]" if serial else hostname
    if serial:
        return serial
    return f"Unidentified ({PurePosixPath(rel_path).name})"
