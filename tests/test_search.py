import pytest

from bundlesearch.search import (
    MAX_CONTEXT,
    build_matchers,
    highlight,
    parse_context,
    parse_terms,
    search_lines,
)

LINES = [f"line {i}" for i in range(1, 21)]


def test_parse_terms_dedupes_and_strips():
    assert parse_terms(" a \n\nb\na\n") == ["a", "b"]


def test_parse_context_bounds():
    assert parse_context("") == 3
    assert parse_context("0") == 0
    with pytest.raises(ValueError):
        parse_context(str(MAX_CONTEXT + 1))
    with pytest.raises(ValueError):
        parse_context("abc")


def test_build_matchers_errors():
    with pytest.raises(ValueError):
        build_matchers([])
    with pytest.raises(ValueError):
        build_matchers(["("], regex=True)


def test_literal_is_escaped():
    m = build_matchers(["10.1.1.1"])
    assert not search_lines(["10x1y1z1"], m, 0)
    assert search_lines(["ip 10.1.1.1/32"], m, 0)


def test_case_sensitivity():
    assert search_lines(["FOO"], build_matchers(["foo"]), 0)
    assert not search_lines(["FOO"], build_matchers(["foo"], case_sensitive=True), 0)


def test_context_window_and_edges():
    blocks = search_lines(LINES, build_matchers(["line 1"], regex=False), 2)
    # "line 1" also matches line 10..19; check first block starts at file start
    assert blocks[0].start == 1


def test_overlapping_windows_merge():
    m = build_matchers([r"^line (5|7)$"], regex=True)
    blocks = search_lines(LINES, m, 2)
    assert len(blocks) == 1
    assert (blocks[0].start, blocks[0].end) == (3, 9)
    assert blocks[0].match_count == 2


def test_separate_windows():
    m = build_matchers([r"^line (2|15)$"], regex=True)
    blocks = search_lines(LINES, m, 1)
    assert [(b.start, b.end) for b in blocks] == [(1, 3), (14, 16)]


def test_end_of_file_edge():
    blocks = search_lines(LINES, build_matchers(["line 20"]), 5)
    assert blocks[0].end == 20


def test_multiple_terms_recorded():
    m = build_matchers(["alpha", "beta"])
    blocks = search_lines(["alpha beta", "gamma"], m, 0)
    assert blocks[0].lines[0].terms == ("alpha", "beta")


def test_highlight_escapes_html():
    m = build_matchers(["web"])
    out = str(highlight('<entry name="web">', m))
    assert "&lt;entry" in out
    assert "<mark>web</mark>" in out
