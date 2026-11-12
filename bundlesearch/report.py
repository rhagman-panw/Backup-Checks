"""Run a search across an extracted bundle and export the results."""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Mapping, Sequence

import defusedxml.minidom

from .bundle import is_binary, iter_files, resolve_device_name
from .redact import redact
from .search import Block, Matcher, search_lines

DEFAULT_MAX_MATCHES = 20_000
_REFORMAT_MIN_SIZE = 20_000
_REFORMAT_LONG_LINE = 5_000


@dataclass
class FileResult:
    device: str
    path: str
    blocks: list[Block]
    reformatted: bool = False

    @property
    def match_count(self) -> int:
        return sum(b.match_count for b in self.blocks)


@dataclass
class Report:
    terms: list[str]
    context: int
    regex: bool
    case_sensitive: bool
    source: str
    files: list[FileResult] = field(default_factory=list)
    files_scanned: int = 0
    truncated: bool = False
    max_matches: int = DEFAULT_MAX_MATCHES
    errors: list[str] = field(default_factory=list)  # collection problems (per device)
    created: datetime = field(default_factory=datetime.now)

    @property
    def total_matches(self) -> int:
        return sum(f.match_count for f in self.files)

    def by_device(self) -> list[tuple[str, list[FileResult]]]:
        groups: dict[str, list[FileResult]] = {}
        for f in self.files:
            groups.setdefault(f.device, []).append(f)
        return sorted(groups.items(), key=lambda kv: (not kv[0].startswith("Panorama"), kv[0].lower()))

    @property
    def device_count(self) -> int:
        return len({f.device for f in self.files})


def _maybe_reformat(data: bytes, text: str) -> tuple[str, bool]:
    """Pretty-print XML stored on very long lines so context lines are meaningful."""
    if len(text) < _REFORMAT_MIN_SIZE or not text.lstrip().startswith("<"):
        return text, False
    if max((len(line) for line in text.splitlines()), default=0) < _REFORMAT_LONG_LINE:
        return text, False
    try:
        pretty = defusedxml.minidom.parseString(data).toprettyxml(indent="  ")  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 - fall back to raw text on any parse problem
        return text, False
    return "\n".join(line for line in pretty.splitlines() if line.strip()), True


def run_search(
    root: Path,
    matchers: Sequence[Matcher],
    *,
    context: int,
    regex: bool = False,
    case_sensitive: bool = False,
    serial_map: Mapping[str, str] | None = None,
    source: str = "",
    max_matches: int = DEFAULT_MAX_MATCHES,
) -> Report:
    report = Report(
        terms=[m.term for m in matchers],
        context=context,
        regex=regex,
        case_sensitive=case_sensitive,
        source=source,
        max_matches=max_matches,
    )
    for path, rel in iter_files(root):
        data = path.read_bytes()
        if is_binary(data):
            continue
        report.files_scanned += 1
        text, reformatted = _maybe_reformat(data, data.decode("utf-8", errors="replace"))
        blocks = search_lines(redact(text).splitlines(), matchers, context)
        if not blocks:
            continue
        device = resolve_device_name(rel, data, serial_map)
        report.files.append(FileResult(device, rel, blocks, reformatted))
        if report.total_matches >= max_matches:
            report.truncated = True
            break
    return report


# ------------------------------------------------------------------- exports
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _cell(value: object) -> str:
    s = str(value)
    return "'" + s if s.startswith(_FORMULA_PREFIXES) else s


def _block_text(block: Block) -> str:
    return "\n".join(
        f"{line.number}{'>' if line.is_match else ':'} {line.text}" for line in block.lines
    )


def to_csv(report: Report) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Device", "File", "Line", "Search Terms", "Matched Line", "Context"])
    for device, files in report.by_device():
        for f in files:
            for block in f.blocks:
                ctx = _block_text(block)
                for line in block.lines:
                    if line.is_match:
                        writer.writerow([
                            _cell(device),
                            _cell(f.path),
                            line.number,
                            _cell("; ".join(line.terms)),
                            _cell(line.text.strip()),
                            _cell(ctx),
                        ])
    return buf.getvalue()


def to_text(report: Report) -> str:
    yes_no = lambda b: "yes" if b else "no"  # noqa: E731
    out = [
        "Panorama Config Bundle Search",
        f"Generated:      {report.created:%Y-%m-%d %H:%M:%S}",
        f"Source:         {report.source}",
        f"Search terms:   {', '.join(report.terms)}",
        f"Options:        context={report.context}, regex={yes_no(report.regex)}, "
        f"case-sensitive={yes_no(report.case_sensitive)}",
        f"Files scanned:  {report.files_scanned}",
        f"Matching lines: {report.total_matches} across {report.device_count} device(s)",
        "Note: secret values (passwords, keys, hashes, SNMP communities) are redacted as ********.",
    ]
    if report.truncated:
        out.append(f"WARNING: search stopped after {report.max_matches} matches; results incomplete.")
    if report.errors:
        out.append(f"Collection errors ({len(report.errors)}):")
        out.extend(f"  - {e}" for e in report.errors)
    out.append("")
    if not report.files:
        out.append("No matches found.")
    for device, files in report.by_device():
        out.append("=" * 78)
        out.append(f"Device: {device}")
        out.append("=" * 78)
        for f in files:
            note = " (XML reformatted for display)" if f.reformatted else ""
            out.append(f"--- File: {f.path} ({f.match_count} matching lines){note}")
            for i, block in enumerate(f.blocks):
                if i:
                    out.append("  --")
                for line in block.lines:
                    marker = ">" if line.is_match else " "
                    out.append(f"{marker} {line.number:>7}: {line.text}")
            out.append("")
    return "\n".join(out) + "\n"
