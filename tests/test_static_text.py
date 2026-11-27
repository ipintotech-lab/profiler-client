"""The static files are inlined into web pages that pin each inline script by its CSP hash.

The HTML parser rewrites some characters inside a <script> or <style> (a NUL becomes U+FFFD, a CR becomes LF),
so the browser hashes different text from what the server hashed and blocks the whole script. A raw NUL in
report.js did exactly that: every report in the web app opened blank. Write such characters as escapes
(``\\x00`` in JS) instead.
"""
from pathlib import Path

import pytest

STATIC = Path(__file__).parent.parent / "src" / "profiler_client" / "static"
FILES = sorted(p for p in STATIC.iterdir() if p.suffix in {".js", ".css", ".html"})


def test_there_are_static_files():
    assert any(p.name == "report.js" for p in FILES)


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_no_character_the_html_parser_rewrites(path):
    # a Windows checkout (core.autocrlf) turns LF into CRLF: that is the checkout, not the source, and the
    # server reads the files as text, which turns CRLF back into LF. A lone CR or any other control byte is ours.
    data = path.read_bytes().replace(b"\r\n", b"\n")
    bad = sorted({b for b in data if b < 0x20 and b not in (0x09, 0x0A)})
    assert not bad, f"{path.name} holds control bytes {[hex(b) for b in bad]}: write them as escapes"
