"""Line based search with context windows and HTML highlighting."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from markupsafe import Markup, escape

MAX_TERMS = 50
MAX_TERM_LEN = 500
MAX_CONTEXT = 20
MAX_LINE_CHARS = 2000


@dataclass(frozen=True)
class Matcher:
    term: str
    pattern: re.Pattern


@dataclass
class ContextLine:
    number: int  # 1-based
    text: str
    terms: tuple[str, ...] = ()

    @property
    def is_match(self) -> bool:
        return bool(self.terms)


@dataclass
class Block:
    lines: list[ContextLine] = field(default_factory=list)

    @property
    def start(self) -> int:
        return self.lines[0].number

    @property
    def end(self) -> int:
        return self.lines[-1].number

    @property
    def match_count(self) -> int:
        return sum(1 for line in self.lines if line.is_match)


def parse_terms(text: str) -> list[str]:
    """Split textarea input into unique, non-empty terms (one per line)."""
    seen: dict[str, None] = {}
    for raw in (text or "").splitlines():
        term = raw.strip()
        if term:
            seen.setdefault(term, None)
    return list(seen)


def parse_context(value: object, default: int = 3) -> int:
    try:
        n = int(str(value).strip()) if value not in (None, "") else default
    except ValueError:
        raise ValueError("Context lines must be a whole number.") from None
    if n < 0 or n > MAX_CONTEXT:
        raise ValueError(f"Context lines must be between 0 and {MAX_CONTEXT}.")
    return n


def build_matchers(
    terms: Iterable[str], *, regex: bool = False, case_sensitive: bool = False
) -> list[Matcher]:
    flags = 0 if case_sensitive else re.IGNORECASE
    matchers: list[Matcher] = []
    seen: set[str] = set()
    for raw in terms:
        term = raw.strip()
        if not term or term in seen:
            continue
        seen.add(term)
        if len(term) > MAX_TERM_LEN:
            raise ValueError(f"Search value is too long (max {MAX_TERM_LEN} characters).")
        source = term if regex else re.escape(term)
        try:
            pattern = re.compile(source, flags)
        except re.error as exc:
            raise ValueError(f"Invalid regular expression {term!r}: {exc}") from None
        matchers.append(Matcher(term, pattern))
    if not matchers:
        raise ValueError("Enter at least one search value.")
    if len(matchers) > MAX_TERMS:
        raise ValueError(f"Too many search values (max {MAX_TERMS}).")
    return matchers


def _clip(text: str, matchers: Sequence[Matcher], limit: int = MAX_LINE_CHARS) -> str:
    """Shorten very long lines, keeping the first match visible."""
    if len(text) <= limit:
        return text
    anchor = 0
    for m in matchers:
        mo = m.pattern.search(text)
        if mo:
            anchor = mo.start()
            break
    begin = max(0, anchor - limit // 2)
    end = min(len(text), begin + limit)
    begin = max(0, end - limit)
    return ("…" if begin > 0 else "") + text[begin:end] + ("…" if end < len(text) else "")


def search_lines(lines: Sequence[str], matchers: Sequence[Matcher], context: int) -> list[Block]:
    """Find lines matching any matcher; return merged blocks with ``context`` lines around hits."""
    hits: dict[int, tuple[str, ...]] = {}
    for idx, line in enumerate(lines):
        terms = tuple(m.term for m in matchers if m.pattern.search(line))
        if terms:
            hits[idx] = terms
    if not hits:
        return []

    last = len(lines) - 1
    windows: list[list[int]] = []
    for idx in hits:  # dict preserves ascending insertion order
        start, end = max(0, idx - context), min(last, idx + context)
        if windows and start <= windows[-1][1] + 1:
            windows[-1][1] = max(windows[-1][1], end)
        else:
            windows.append([start, end])

    blocks = []
    for start, end in windows:
        blocks.append(
            Block(
                [
                    ContextLine(i + 1, _clip(lines[i], matchers), hits.get(i, ()))
                    for i in range(start, end + 1)
                ]
            )
        )
    return blocks


def highlight(text: str, matchers: Sequence[Matcher]) -> Markup:
    """HTML-escape ``text`` and wrap every match in ``<mark>``."""
    spans: list[tuple[int, int]] = []
    for m in matchers:
        for mo in m.pattern.finditer(text):
            if mo.end() > mo.start():
                spans.append((mo.start(), mo.end()))
    if not spans:
        return Markup(escape(text))
    spans.sort()
    merged = [list(spans[0])]
    for s, e in spans[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    out: list[Markup] = []
    pos = 0
    for s, e in merged:
        out.append(escape(text[pos:s]))
        out.append(Markup("<mark>") + escape(text[s:e]) + Markup("</mark>"))
        pos = e
    out.append(escape(text[pos:]))
    return Markup("").join(out)
