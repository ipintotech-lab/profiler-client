"""The light and dark themes, and the layout fixes that came with them.

One set of colour variables per theme in report.css (light on :root, dark on [data-theme="dark"]), so every part of
the report follows the theme and none keeps a colour of its own; print is always light. static/theme.js picks the
theme and keeps the reader's choice: the only thing any script here stores. The header says what is on screen.
"""
import gzip
import json
import re
from pathlib import Path

import pytest
from test_agent_flow import FLOW, _load, _plain, _render

from profiler_client.report import render_html

STATIC = Path(__file__).parent.parent / "src" / "profiler_client" / "static"
CSS = (STATIC / "report.css").read_text(encoding="utf-8")
THEME = (STATIC / "theme.js").read_text(encoding="utf-8")
JS = (STATIC / "report.js").read_text(encoding="utf-8")


def _block(selector: str) -> str:
    start = CSS.index(selector + " {") + len(selector) + 2
    return CSS[start:CSS.index("}", start)]


def _tokens(block: str) -> dict:
    return dict(re.findall(r"--o7-([\w-]+):\s*(#[0-9a-fA-F]{6})", block))


def _contrast(a: str, b: str) -> float:
    def lum(h):
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= .03928 else ((x + .055) / 1.055) ** 2.4 for x in c]
        return .2126 * c[0] + .7152 * c[1] + .0722 * c[2]
    hi, lo = sorted([lum(a), lum(b)], reverse=True)
    return (hi + .05) / (lo + .05)


LIGHT, DARK = _tokens(_block(":root")), _tokens(_block(':root[data-theme="dark"]'))
PRINT = _tokens(CSS[CSS.index("@media print {\n  :root[data-theme]"):])


def test_both_themes_set_the_same_variables_and_print_is_the_light_one():
    assert LIGHT and LIGHT.keys() == DARK.keys()
    assert {k: PRINT[k] for k in LIGHT} == LIGHT


def test_no_part_of_the_report_keeps_a_colour_of_its_own():
    # outside the two sets of variables, colours come from them: a new hex or rgba would miss one theme
    rest = CSS[CSS.index(".sheet {\n  --paper"):]
    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", rest)
    assert not re.findall(r"rgba?\(|#[0-9a-fA-F]{6}\b", JS)  # the live demo's tiles and canvas read the variables too


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("text,on", [
    ("ink", "sheet"), ("ink", "paper"), ("ink-2", "sheet"), ("ink-2", "paper"), ("accent", "sheet"), ("accent", "accent-soft"),
    ("on-accent", "accent"), ("on-strong", "estimate"), ("on-strong", "bad"), ("on-strong", "shared"), ("on-strong", "accent-deep"),
    ("on-strong", "ink-2"), ("estimate", "estimate-bg"), ("bad", "bad-bg"), ("shared", "shared-bg"), ("save", "save-bg"),
    ("save", "sheet"), ("up", "sheet"), ("down", "sheet"), ("ink", "f3"), ("bad-deep", "f3")])
def test_text_and_badges_meet_wcag_aa_in_both_themes(theme, text, on):
    t = LIGHT if theme == "light" else DARK
    assert _contrast(t[text], t[on]) >= 4.5, (theme, text, on)


def test_the_theme_follows_the_system_until_the_reader_picks_one_and_stores_only_that():
    assert "prefers-color-scheme: dark" in THEME
    assert re.findall(r"localStorage\?\.(\w+)\(", THEME) == ["getItem", "setItem"]
    assert THEME.count("try {") >= 2  # a file:// page or a locked-down browser that refuses storage still themes
    assert "localStorage" not in JS  # the renderer, which holds the names, stores nothing


def _meta(report: dict, restored: int = 0) -> list:
    page = render_html(report, "t", restored=restored)
    start = page.index('id="report-data">') + len('id="report-data">')
    return json.loads(page[start:page.index("</script>", start)])["meta"]


def test_the_header_says_names_were_sent_as_they_are_when_no_name_is_a_pseudonym():
    # the agent-flow sample was prepared with --keep-names: its names are in clear; the pseudonyms left are event
    # ids and output values, which are not names
    with gzip.open(FLOW, "rt", encoding="utf-8") as f:
        report = json.load(f)
    assert _meta(report) == ["Opened on your machine", "names shown as they were sent"]


def test_the_header_says_pseudonyms_are_shown_only_when_a_name_is_one():
    token = "h:" + "7" * 64
    graph = {"run_count": 1, "edges": [], "nodes": [{"id": token, "step": token, "kind": "step", "occurrences": 1, "cost": 0}]}
    assert _meta({"graph": graph}) == ["Opened on your machine", "no names restored, pseudonyms shown"]
    assert _meta({"graph": graph}, restored=3) == ["Opened on your machine", "1 name not matched, shown as pseudonyms"]
    assert _meta({"graph": {**graph, "nodes": []}}, restored=3) == ["Opened on your machine"]


def test_the_page_has_the_toggle_beside_print():
    page = render_html({}, "t")
    assert THEME in page and "OpenlyTheme.init();" in page
    assert page.index("OpenlyTheme.init();") < page.rindex("ProfilerReport.render(")  # themed before it is drawn
    assert "append(print, OpenlyTheme.toggle())" in page


# --- the layout fixes ------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def loan():
    return _load(FLOW)


def test_a_flow_box_name_is_never_cut_and_its_full_name_is_its_tooltip(loan):
    base = _render(loan, [{"tab": "flow"}])["flowBase"]["text"]
    assert "…" not in base
    for name in ("route_after_compliance", "document-collection", "llm_call ← get_application"):
        assert f"<title>{name}</title>" in base
    # a long name is on two lines, split at a separator
    assert ">route_after_</text>" in base and ">compliance</text>" in base


def test_each_handoff_has_its_own_ends_so_its_share_sits_beside_it(loan):
    base = _render(loan, [{"tab": "flow"}])["flowBase"]["text"]
    starts = re.findall(r'class="fl-hand"[^>]*d="M([\d.]+) ([\d.]+)', base)
    ends = re.findall(r'class="fl-hand"[^>]*d="M[^"]*, ([\d.]+) ([\d.]+)"', base)
    assert len(set(starts)) == len(starts) and len(set(ends)) == len(ends)
    shares = re.findall(r'class="fl-share"[^>]*>(\d+)%<', base)
    assert sorted(map(int, shares)) == [6, 17, 25, 75, 83]


def test_one_workflow_fills_the_live_demos_row_and_the_remarks_scroll_beside_the_diagram(loan):
    out = _render(loan, [{"tab": "live"}])
    assert out["liveHeat"]["dataset"]["n"] == "1"
    assert '.lv-heat[data-n="1"] { grid-template-columns: minmax(0, 1fr); }' in CSS
    assert 'class="fl-card fl-side"><div class="fl-sidein">' in JS
    assert _plain(out["liveHeat"]["text"]).count("saveable") == 1
