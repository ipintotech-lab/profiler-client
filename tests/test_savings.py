"""The savings overview, the Openly7 brand and the name map (issue #59), run in Node with a stand-in page.

The fixture is a real result of the server's overview job over the six synthetic agents
(``rawdata_gen``, ``workflow_version`` 1.0.0), prepared by this client without --keep-names, with the
name map ``profiler-client names`` exported for it. Every figure the page shows must add up: the
headline is the workflow rows' sum and the risk tiers' sum, the top actions never exceed the headline
(here they are exactly the ready saving), "Also found" adds to nothing, and the monthly projection's
parts add up to what it shows. And the page must look the same restored on this machine (``open``)
and restored in a browser from the name map (the Openly7 page).
"""
import copy
import gzip
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from profiler_client import cli
from profiler_client.pseudonym import TOKEN
from profiler_client.report import NAME_MAP_FORMAT, render_html, translate, view

STATIC = Path(__file__).parent.parent / "src" / "profiler_client" / "static"
JS = STATIC / "report.js"
DATA = Path(__file__).parent / "data" / "savings-overview"

# a page with no browser: each [data-r] slot keeps what the report writes into it, and whether it's hidden
HARNESS = """
const el = () => { const kids = new Map(); return { innerHTML: '', textContent: '', hidden: false, dataset: {}, style: {},
  value: '', classList: { toggle() {}, add() {}, remove() {} }, on: {}, addEventListener(t, f) { this.on[t] = f; },
  setAttribute(k, v) { this['@' + k] = v; }, kids,
  querySelector: sel => { if (!kids.has(sel)) kids.set(sel, el()); return kids.get(sel); },
  querySelectorAll: () => [], focus() {}, scrollIntoView() {}, childNodes: [], append() {} }; };
const text = e => [e.innerHTML || e.textContent, ...[...e.kids.values()].map(text)].join('');
const slots = new Map(), root = el(), tools = el();
root.querySelector = sel => { const k = (sel.match(/data-r="?([^"\\]]+)/) || [])[1];
  if (!k) return sel === '.tools' ? tools : null; if (!slots.has(k)) slots.set(k, el()); return slots.get(k); };
globalThis.window = {};
globalThis.CSS = { escape: x => x };
require(process.argv[1]);
let input = ''; process.stdin.on('data', d => { input += d; }).on('end', () => {
  let data = JSON.parse(input), restore = null;
  if (process.env.NAMES) {  // what the Openly7 page does with a name map the reader loads
    const r = window.ProfilerReport.restoreNames(data, JSON.parse(require('fs').readFileSync(process.env.NAMES, 'utf8')));
    restore = { restored: r.restored, unmatched: r.unmatched, unlisted: r.unlisted, error: r.error };
    if (r.data) data = r.data;
  }
  window.ProfilerReport.render(root, data, JSON.parse(process.env.OPTS || '{}'));
  if (process.env.RUNS) { const s = slots.get('svRuns'); s.value = process.env.RUNS; s.on.input(); }
  const out = Object.fromEntries([...slots].map(([k, e]) => [k, { text: text(e), hidden: e.hidden }]));
  console.log(JSON.stringify({ slots: out, restore }));
});
"""


def _render(data: dict, *, opts: dict | None = None, runs: int | None = None, names: Path | None = None) -> dict:
    env = {**os.environ, "OPTS": json.dumps(opts or {})}
    if runs is not None:
        env["RUNS"] = str(runs)
    if names is not None:
        env["NAMES"] = str(names)
    out = subprocess.run(["node", "-e", HARNESS, str(JS)], input=json.dumps(data), capture_output=True,
                         text=True, check=True, env=env, encoding="utf-8")
    return json.loads(out.stdout)


def _fixture() -> tuple[dict, dict]:
    with gzip.open(DATA / "report.json.gz", "rt", encoding="utf-8") as f:
        report = json.load(f)
    with gzip.open(DATA / "names.json.gz", "rt", encoding="utf-8") as f:
        names = json.load(f)
    return report, names


@pytest.fixture(scope="module")
def fixture():
    return _fixture()


@pytest.fixture(scope="module")
def page(fixture):
    """The page as `profiler-client open` draws it: names restored on this machine, then view()."""
    report, names = fixture
    restored = view(translate(report, names["names"], set(), set()))
    return _render(restored, opts={"brand": True})["slots"]


def _text(slot: dict) -> str:
    return re.sub(r"<[^>]+>", "", slot["text"])


def _money(text: str) -> list[float]:
    return [float(m.replace(",", "")) for m in re.findall(r"\$([0-9][0-9,]*(?:\.[0-9]+)?)", text)]


def _counted(report: dict) -> list[dict]:
    return [f for r in report["reports"].values() for f in r["summary"]["findings"] if f.get("counted")]


# --- the figures add up ---------------------------------------------------------------------------

def test_the_headline_is_the_sum_of_the_workflow_rows(fixture, page):
    o = fixture[0]["overview"]
    assert sum(r["replaceable"] for r in o["workflows"]) == pytest.approx(o["totals"]["replaceable"])
    assert sum(r["spend"] for r in o["workflows"]) == pytest.approx(o["totals"]["spend"])
    assert _text(page["svBig"]).startswith("$1.78 of $8.23 could be saved")
    assert "22%" in _text(page["svLead"])


def test_the_headline_is_the_sum_of_the_counted_findings_and_of_the_risk_tiers(fixture, page):
    report = fixture[0]
    rt, total = report["overview"]["risk_totals"], report["overview"]["totals"]["replaceable"]
    assert sum(f["saving"] for f in _counted(report)) == pytest.approx(total)
    assert rt["low"]["saving"] + rt["medium"]["saving"] + rt["high"]["saving"] == pytest.approx(total)
    legend = _text(page["svLegend"])
    assert "$1.32 ready to save · low risk" in legend and "$0.463 review first" in legend and "$6.45 needed work" in legend


def test_the_top_actions_are_exactly_the_ready_saving(fixture, page):
    rt = fixture[0]["overview"]["risk_totals"]
    cards = page["svActions"]["text"]
    assert cards.count('class="sv-act"') == 3
    assert cards.count("Ready · low risk") == 3 and "Review first" not in cards
    amounts = [float(m) for m in re.findall(r'<p class="money"><b>\$([0-9.]+)</b>', cards)]
    assert sum(amounts) == pytest.approx(rt["low"]["saving"], abs=0.002)  # each shown to the tenth of a cent
    assert "These 3 actions are $1.32 of the $1.78, all of the ready saving." in _text(page["svActionsNote"])


def test_one_steps_llm_calls_split_by_what_came_before_are_one_action(page):
    first = page["svActions"]["text"].split('class="sv-act"')[1]
    assert "(3 places)" in first and "$0.832" in first
    assert "the prompt" in first and "fetch_diff" in first and "run_linter" in first


def test_actions_never_exceed_the_headline_and_ready_comes_first(fixture):
    # with nothing ready, review-first actions lead, and the note never claims "all of the ready saving"
    report = copy.deepcopy(fixture[0])
    for f in (f for r in report["reports"].values() for f in r["summary"]["findings"]):
        if f.get("risk") == "low":
            f["risk"] = "medium"
    rt = report["overview"]["risk_totals"]
    rt["medium"] = {k: rt["medium"][k] + rt["low"][k] for k in ("count", "counted", "saving")}
    rt["low"] = {"count": 0, "counted": 0, "saving": 0}
    slots = _render(report)["slots"]
    cards = slots["svActions"]["text"]
    assert "Ready · low risk" not in cards and cards.count("Review first") == 3
    amounts = [float(m) for m in re.findall(r'<p class="money"><b>\$([0-9.]+)</b>', cards)]
    assert sum(amounts) <= report["overview"]["totals"]["replaceable"] + 1e-9
    assert "ready saving" not in _text(slots["svActionsNote"])
    assert "$0 ready to save" in _text(slots["svLegend"])


def test_also_found_is_shown_but_never_added_to_any_sum(fixture, page):
    report = fixture[0]
    also = _text(page["svAlso"])
    assert also.count("Also found, adds nothing to the total") == 2
    assert "Script a fixed sequence" in also and "$0.570 on its own" in also
    # the chain is not counted, and nothing counted is listed as "also found"
    chain = next(f for r in report["reports"].values() for f in r["summary"]["findings"]
                 if f["type"] == "EXTRACT_DETERMINISTIC_SUBSTEP" and f["saving"] > 0.5)
    assert not chain["counted"] and chain["held_out"] == "overlap"
    assert sum(f["saving"] for f in _counted(report)) == pytest.approx(report["overview"]["totals"]["replaceable"])
    note = _text(page["svActionsNote"])
    assert "$0.570" not in note and "$1.32" in note


@pytest.mark.parametrize("runs", [100_000, 37_000, 19_000, 2_500, 1_234_567])
def test_the_monthly_projection_adds_up_at_every_volume(fixture, runs):
    report, names = fixture
    slots = _render(view(translate(report, names["names"], set(), set())), runs=runs)["slots"]
    rows = [m for m in re.findall(r"≈ \$([0-9,.]+) / month", slots["svActions"]["text"])]
    ready = _money(_text(slots["svOutReady"]))[0]
    assert sum(float(r.replace(",", "")) for r in rows) == pytest.approx(ready, abs=0.006)
    # one unit for every figure: all whole dollars, or all cents
    whole = [("." not in r) for r in rows] + ["." not in _text(slots["svOut"]).split()[0]]
    assert len(set(whole)) == 1
    assert _money(_text(slots["svOut"]))[0] >= ready


def test_without_a_volume_the_projection_shows_the_saving_per_run(page):
    assert _text(page["svOut"]) == "$0.0052 per run"
    assert "Type your runs per month" in _text(page["svOutReady"])
    assert "Not a forecast, and nothing has been replayed yet." in _text(page["svNote"])


# --- words, not color alone -------------------------------------------------------------------------

def test_every_risk_is_named_in_words(page):
    pills = re.findall(r'<span class="pill (\w+)">([^<]+)</span>', page["svActions"]["text"] + page["svInvestigate"]["text"])
    assert pills and all(word in {"Ready · low risk", "Review first", "Investigate"} for _, word in pills)
    assert ("invest", "Investigate") in pills
    assert "14 findings to investigate" in _text(page["svInvestigate"])


def test_a_dataset_with_no_cost_says_so_and_shows_no_figures():
    data = {"overview": {"workflows": [{"name": "wf", "runs": 3, "spend": 0, "replaceable": 0, "findings": 0}],
                         "totals": {"spend": 0, "replaceable": 0, "findings": 0}}, "reports": {"wf": {"summary": {}}}}
    slots = _render(data)["slots"]
    assert "No cost is recorded" in _text(slots["svLead"]) and slots["svStack"]["hidden"] and slots["svProj"]["hidden"]
    assert slots["svActionsSec"]["hidden"]


def test_a_result_from_before_risk_tiers_shows_one_bar():
    data = {"overview": {"workflows": [{"name": "wf", "runs": 4, "spend": 10.0, "replaceable": 2.0, "findings": 1}],
                         "totals": {"spend": 10.0, "replaceable": 2.0, "findings": 1}}, "reports": {"wf": {"summary": {}}}}
    slots = _render(data)["slots"]
    assert "$2.00 could be saved" in _text(slots["svLegend"]) and "ready" not in _text(slots["svLegend"])
    assert slots["svInvestigate"]["hidden"]


# --- the brand ----------------------------------------------------------------------------------

def test_the_brand_shows_only_when_the_host_asks(fixture):
    data = view(fixture[0])
    shown, plain = _render(data, opts={"brand": True})["slots"], _render(data)["slots"]
    assert not shown["brand"]["hidden"] and not shown["brandFoot"]["hidden"]
    assert plain["brand"]["hidden"] and plain["brandFoot"]["hidden"]  # the Openly7 site has its own header
    assert "Openly7 sees pseudonyms only" in JS.read_text(encoding="utf-8")


def test_open_and_the_app_ask_for_the_brand(fixture):
    page_html = render_html(fixture[0], "report")
    assert "brand: true" in page_html


# --- the same display on both sides: restored here, or in a browser from the name map ---------------

def test_the_report_looks_the_same_restored_here_and_in_a_browser(fixture, tmp_path):
    report, names = fixture
    here = _render(view(translate(report, names["names"], set(), set())))["slots"]
    (tmp_path / "names.json").write_text(json.dumps(names), encoding="utf-8")
    browser = _render(report, names=tmp_path / "names.json")
    assert browser["restore"]["restored"] > 0 and browser["restore"]["unlisted"] == []
    assert browser["slots"] == here


def test_a_name_map_restores_every_name_it_holds(fixture, tmp_path):
    report, names = fixture
    (tmp_path / "names.json").write_text(json.dumps(names), encoding="utf-8")
    shown = json.dumps(_render(report, names=tmp_path / "names.json")["slots"])
    assert not set(TOKEN.findall(shown)) & set(names["names"])  # what stays a pseudonym was never a name


def test_a_name_map_for_another_dataset_restores_nothing(fixture, tmp_path):
    report, names = fixture
    (tmp_path / "other.json").write_text(json.dumps({**names, "datasets": ["f" * 64]}), encoding="utf-8")
    out = _render(report, names=tmp_path / "other.json")
    assert "another dataset" in out["restore"]["error"]
    shown = json.dumps(out["slots"])
    assert not any(name in shown for name in ("pull-request-review", "plan_next_step", "decide_verdict"))
    (tmp_path / "junk.json").write_text(json.dumps({"names": {}}), encoding="utf-8")
    assert "isn’t a name map" in _render(report, names=tmp_path / "junk.json")["restore"]["error"]


def test_the_page_never_hashes_or_sends_anything():
    # names are swapped in the browser from the map, never hashed there and never sent
    source = JS.read_text(encoding="utf-8")
    # (navigator.clipboard, for the Copy buttons, writes to the reader's own clipboard and sends nothing)
    for forbidden in ("crypto", "subtle", "Math.imul", "fetch(", "XMLHttpRequest", "sendBeacon", "WebSocket",
                      "EventSource", "localStorage", "document.cookie", "new Image", "importScripts"):
        assert forbidden not in source, forbidden


def test_the_names_command_exports_this_datasets_map(tmp_path, monkeypatch, transcript_file, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert cli.main(["prepare", str(transcript_file), "-o", str(tmp_path / "ready")]) == 0
    seal = json.loads((tmp_path / "ready" / "events.jsonl").read_text(encoding="utf-8").splitlines()[0])
    digest = seal["profiler_client_seal"]["payload_sha256"]
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"payload_sha256": digest}), encoding="utf-8")
    assert cli.main(["names", str(report)]) == 0
    exported = json.loads((tmp_path / "report.names.json").read_text(encoding="utf-8"))
    assert exported["format"] == NAME_MAP_FORMAT and exported["datasets"] == [digest] and exported["names"]
    assert "never uploaded" in capsys.readouterr().out
    # a report of another dataset gets nothing, and says so
    report.write_text(json.dumps({"payload_sha256": "e" * 64}), encoding="utf-8")
    assert cli.main(["names", str(report), "-o", str(tmp_path / "x.json")]) == 1
    assert "no name list on this machine" in capsys.readouterr().err and not (tmp_path / "x.json").exists()


# --- view(): what the page is given -------------------------------------------------------------------

def test_view_keeps_what_the_savings_page_and_all_findings_draw(fixture):
    # before #59, view() dropped findings and risk totals, so open never showed All findings or the risk tiles
    viewed = view(fixture[0])
    assert set(viewed["overview"]["risk_totals"]) == {"low", "medium", "high"}
    for report in viewed["reports"].values():
        summary = report["summary"]
        assert "risk_totals" in summary and summary["findings"]
        for f in summary["findings"]:
            assert set(f) <= {"type", "title", "nodes", "action", "saving", "observations", "risk", "risk_reasons",
                              "counted", "covered_by", "held_out", "agent"}


def test_view_keeps_what_marks_an_estimated_cost(fixture):
    # the estimate mark reads the cost gap's status, and the overview its unpriced count: view() dropped
    # the status before, so a page from open was never marked
    report = copy.deepcopy(fixture[0])
    wf = next(iter(report["reports"]))
    report["reports"][wf]["data_quality"] = {"gaps": [{"key": "cost", "label": "cost ($)", "coverage": 0, "status": "estimated",
                                                       "effect": "Cost is estimated.", "unpriced": 7}]}
    (gap,) = view(report)["reports"][wf]["data_quality"]["gaps"]
    assert gap == {"key": "cost", "label": "cost ($)", "status": "estimated", "effect": "Cost is estimated.", "unpriced": 7}
    slots = _render(view(report))["slots"]
    assert not slots["svEstimate"]["hidden"]
    assert "7 calls have no price" in _text(slots["svEstimate"])
