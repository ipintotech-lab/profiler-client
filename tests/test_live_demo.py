"""The overview's Live demo tab (issue #59): a simulation built from the report's own numbers.

Its calls are each workflow's counted findings (their calls and saving per call) plus one group for
the rest of its calls and spend, drawn by calls. So over many calls each workflow's share of spend
and of saveable spend must be the report's, the labels must be the report's actions, it must repeat
exactly from a seed, pause while hidden, and update its tiles in place.
"""
import gzip
import json
import subprocess
from pathlib import Path

import pytest
from test_agent_flow import _render

from profiler_client.report import translate, view

JS = Path(__file__).parent.parent / "src" / "profiler_client" / "static" / "report.js"
DATA = Path(__file__).parent / "data" / "savings-overview"

SIMULATE = """
globalThis.window = {}; globalThis.CSS = { escape: x => x };
require(process.argv[1]);
let input = ''; process.stdin.on('data', d => { input += d; }).on('end', () => {
  const { n, seed } = JSON.parse(process.argv[2]);
  console.log(JSON.stringify(window.ProfilerReport.liveSimulate(JSON.parse(input), n, seed)));
});
"""


def _simulate(data: dict, n: int, seed: int = 1) -> dict:
    out = subprocess.run(["node", "-e", SIMULATE, str(JS), json.dumps({"n": n, "seed": seed})], input=json.dumps(data),
                         capture_output=True, text=True, check=True, encoding="utf-8")
    return json.loads(out.stdout)


@pytest.fixture(scope="module")
def report():
    """The six synthetic agents' overview, names restored as profiler-client open shows it."""
    with gzip.open(DATA / "report.json.gz", "rt", encoding="utf-8") as f:
        raw = json.load(f)
    with gzip.open(DATA / "names.json.gz", "rt", encoding="utf-8") as f:
        names = json.load(f)["names"]
    return view(translate(raw, names, set(), set()))


@pytest.fixture(scope="module")
def run(report):
    return _simulate(report, 40_000)


def test_each_workflows_share_of_spend_is_the_reports(report, run):
    rows = {r["name"]: r for r in report["overview"]["workflows"]}
    total = sum(r["spend"] for r in rows.values())
    simulated = sum(w["spend"] for w in run["workflows"].values())
    for name, w in run["workflows"].items():
        assert w["spend"] / simulated == pytest.approx(rows[name]["spend"] / total, abs=0.015), name


def test_each_workflows_saveable_share_is_the_reports(report, run):
    for row in report["overview"]["workflows"]:
        w = run["workflows"][row["name"]]
        assert w["save"] / w["spend"] == pytest.approx(row["replaceable"] / row["spend"], abs=0.02), row["name"]


def test_the_overall_saveable_share_is_the_headline(report, run):
    t = report["overview"]["totals"]
    spend = sum(w["spend"] for w in run["workflows"].values())
    save = sum(w["save"] for w in run["workflows"].values())
    assert save / spend == pytest.approx(t["replaceable"] / t["spend"], abs=0.01)  # 22%, not the 16% of an even pick


def test_the_labels_are_the_reports_actions(report, run):
    # each counted finding's calls are labelled by what the report says to do with them
    def label(f):
        if f["type"] in ("CONDITIONAL_LLM_BYPASS", "PARTIAL_LLM_BYPASS"):
            return "lookup could decide" if "lookup" in f["action"].lower() else "rule could decide"
        return {"CACHE_RESULT": "repeat: reuse", "CROSS_AGENT_DUPLICATE": "fetch once"}.get(f["type"], "saveable")
    expected = {f"{f['title']} · {label(f)}" for r in report["reports"].values() for f in r["summary"]["findings"]
                if f.get("counted") and f.get("saving") and f.get("observations")}
    assert set(run["tags"]) == expected
    assert "decide_verdict · rule could decide" in expected and "decide_retry · lookup could decide" in expected


def test_workflows_with_nothing_counted_are_never_saveable(report, run):
    for row in report["overview"]["workflows"]:
        if not row["replaceable"]:
            assert run["workflows"][row["name"]]["save"] == 0


def test_a_seed_repeats_the_demo_exactly(report):
    assert _simulate(report, 3000, seed=5) == _simulate(report, 3000, seed=5)
    assert _simulate(report, 3000, seed=5) != _simulate(report, 3000, seed=6)


# --- the tab ---------------------------------------------------------------------------------------

def test_the_tab_says_it_simulates_and_fills_its_dashboard(report):
    out = _render(report, [{"tab": "live"}])
    source = JS.read_text(encoding="utf-8")
    assert "it <b>simulates</b> calls with this report’s own numbers. Nothing streams anywhere." in source
    assert out["liveCalls"]["text"] == "900 calls"  # the warm-up
    assert out["liveShare"]["text"].endswith("% of spend") and out["liveSpend"]["text"].startswith("$")
    assert out["liveHeat"]["text"].count('class="lv-tile"') == len(report["overview"]["workflows"])
    for ticker in ("PRREV", "LQUAL", "EIRES", "CRAGE", "STTRI", "DIRES"):  # initials, then the rest of the last word
        assert f'data-sym="{ticker}"' in out["liveHeat"]["text"], ticker
    assert "the report’s 22%" in out["liveMeterNote"]["text"]


def test_tiles_are_built_once_and_updated_in_place(report):
    # repaints write into each tile, never the heatmap itself, so a focused tile keeps its focus
    out = _render(report, [{"tab": "live"}, {"wait": 1200}])
    assert 'lv-pct">0%' in out["liveHeat"]["text"]  # the heatmap still holds the markup it was built with


def test_it_runs_only_while_its_tab_is_shown(report):
    shown = _render(report, [{"tab": "live"}, {"wait": 1500}])
    assert int(shown["liveCalls"]["text"].split()[0].replace(",", "")) > 900
    hidden = _render(report, [{"wait": 1500}])  # the savings tab is shown: the demo waits
    assert hidden["liveCalls"]["text"] == "900 calls"
    left = _render(report, [{"tab": "live"}, {"wait": 600}, {"tab": "savings"}, {"wait": 1200}])
    left_again = _render(report, [{"tab": "live"}, {"wait": 600}, {"tab": "savings"}])
    calls = lambda o: int(o["liveCalls"]["text"].split()[0].replace(",", ""))
    assert calls(left) - calls(left_again) < 20  # stopped when its tab was left (a tick or two of timing)


def test_it_waits_while_the_page_is_hidden(report):
    # its tab is shown, but another browser tab is in front: nothing runs that nobody sees
    out = _render(report, [{"tab": "live"}, {"wait": 1500}], page_hidden=True)
    assert out["liveCalls"]["text"] == "900 calls"


def test_a_report_with_no_cost_has_no_demo():
    data = {"overview": {"workflows": [{"name": "wf", "runs": 3, "spend": 0, "replaceable": 0, "findings": 0}],
                         "totals": {"spend": 0, "replaceable": 0, "findings": 0}}, "reports": {"wf": {"summary": {}}}}
    out = _render(data, [{"tab": "live"}])
    assert "No live demo" in out["liveTerm"]["text"]


def test_the_live_tab_is_reached_by_keys(report):
    out = _render(report, [{"key": "End", "from": "savings"}])
    assert not out["viewLive"]["hidden"] and out["viewSavings"]["hidden"] and out["viewFlow"]["hidden"]
