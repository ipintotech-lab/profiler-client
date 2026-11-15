"""The report page, run in Node with a stand-in for the page: every share of spend it shows for one
step is the same figure, worked out from cost and total cost, on the agent map, the hot spots, the
details panel and the steps table."""
import json
import re
import subprocess
from pathlib import Path

from test_graph import _agent_ev, _decision, _ev

from profiler_client.graph import build_graph

JS = Path(__file__).parent.parent / "src" / "profiler_client" / "static" / "report.js"

# a page with no browser: each [data-r] slot keeps what the report writes into it
HARNESS = """
const el = () => { const kids = new Map(); return { innerHTML: '', textContent: '', hidden: false, dataset: {}, style: {},
  classList: { toggle() {}, add() {}, remove() {} }, addEventListener() {}, setAttribute() {}, kids,
  querySelector: sel => { if (!kids.has(sel)) kids.set(sel, el()); return kids.get(sel); },
  querySelectorAll: () => [], focus() {}, scrollIntoView() {} }; };
const text = e => [e.innerHTML || e.textContent, ...[...e.kids.values()].map(text)].join('');
const slots = new Map(), root = el();
root.querySelector = sel => { const k = (sel.match(/data-r="?([^"\\]]+)/) || [])[1];
  if (!k) return null; if (!slots.has(k)) slots.set(k, el()); return slots.get(k); };
globalThis.window = {};
globalThis.CSS = { escape: x => x };
require(process.argv[1]);
let input = ''; process.stdin.on('data', d => { input += d; }).on('end', () => {
  window.ProfilerReport.render(root, JSON.parse(input), { name: 'wf' });
  console.log(JSON.stringify(Object.fromEntries([...slots].map(([k, e]) => [k, text(e)]))));
});
"""


def _render(report: dict) -> dict:
    out = subprocess.run(["node", "-e", HARNESS, str(JS)], input=json.dumps(report), capture_output=True,
                         text=True, check=True)
    return json.loads(out.stdout)


def _report() -> dict:
    # route_kyc: 4 calls at $1.462, of $10 in all: 58.48%, which a share stored to three places (0.585) shows as 59%
    events = []
    for run in ("r1", "r2", "r3"):
        events += [_agent_ev(run, f"{run}k", "kyc", "kyc_lookup"), _decision(run, f"{run}d1", "route_kyc", "kyc_lookup", 1.462)]
        d = f"{run}d1"
        if run == "r3":
            events += [_agent_ev(run, f"{run}v", "docs", "validate", d), _agent_ev(run, f"{run}k2", "kyc", "kyc_lookup", f"{run}v"),
                       _decision(run, f"{run}d1b", "route_kyc", "kyc_lookup", 1.462)]
            d = f"{run}d1b"
        events += [_agent_ev(run, f"{run}f", "fraud", "balance", d), _agent_ev(run, f"{run}c", "credit", "score", d),
                   _decision(run, f"{run}d2", "route_checks", "balance + score", 1.384),
                   _agent_ev(run, f"{run}l", "letter", "send", f"{run}d2")]
    graph = build_graph(events, "wf", "invokes")["graph"]
    actions = [{"node": "route_kyc", "total_cost": 5.848, "occurrences": 4},
               {"node": "route_checks", "total_cost": 4.152, "occurrences": 3}]
    return {"availability": {"graph": {"available": True}}, "graph": graph,
            "summary": {"totals": {"cost": 10.0, "runs": 3}, "opportunities": [], "next_steps": []},
            "mine": {"by_agent": {"sup": {"actions": actions}}}}


def test_one_share_of_spend_for_a_step_everywhere_it_is_shown():
    report = _report()
    stored = next(d for d in report["graph"]["agent_graph"]["decisions"] if d["id"] == "route_kyc")["share"]
    assert stored == 0.585  # rounded to three places, it would show as 59%
    page = _render(report)
    box = re.search(r'aria-label="Hot spot 1: route_kyc, ([^ ]+) of spend', page["map"])
    figures = re.search(r'data-an="route_kyc"[^>]*>.*?<text class="f"[^>]*>([^ ]+) ·', page["map"])
    hot = re.search(r'<button type="button" data-an="route_kyc">.*?<span class="k">([^ ]+) of spend', page["map"])
    panel = re.search(r"([0-9<]+%) of spend\.", page["amPanel"])
    table = re.search(r'data-node="route_kyc".*?data-label="Cost">[^<]*<span class="note">([^<]+)</span>', page["steps"], re.DOTALL)
    assert [x and x.group(1) for x in (box, figures, hot, panel, table)] == ["58%"] * 5


def test_print_has_the_hot_spots_then_one_section_per_agent_with_spend():
    page = _render(_report())
    hot = re.findall(r"<tr><td class=\"num\">(\d)</td><td>([^<]+)</td><td class=\"num\">([^<]+)</td>", page["amPrint"])
    assert hot == [("1", "route_kyc", "58%"), ("2", "route_checks", "42%")]
    # only the supervisor has spend; the specialists have neither spend nor findings, so get no section
    assert re.findall(r'<div class="agent"><h3>([^<]+)</h3>', page["amPrint"]) == ["sup"]
    # its two steps have no link between them in the trace: a table, not an empty map
    assert '<table class="tbl agsteps">' in page["pm0"] and "<svg" not in page["pm0"]
    assert re.findall(r'<span class="step">([^<]+)</span>', page["pm0"]) == ["route_kyc", "route_checks"]


def _step(type_, nodes, saving, step="declare"):
    return {"type": type_, "step": step, "nodes": nodes, "observations": 4, "saving": saving, "risk": "low", "risk_reasons": []}


def test_the_summary_gives_the_headline_figure_not_the_sum_of_the_rows_repeats():
    report = _report()
    report["summary"]["replaceable"] = {"cost": 2.5, "findings": 2}
    report["summary"]["opportunities"] = [_step("CACHE_RESULT", ["route_kyc"], 3.0), _step("CACHE_RESULT", ["route_checks"], 1.0)]
    says = _render(report)["says"]
    assert "<b>$2.50</b> (25%) went to calls a rule, a script or a cache could replace, from 2 findings" in says
    assert "$4.00" not in says


def test_two_findings_on_the_same_calls_are_one_card_with_one_amount():
    report = _report()
    report["summary"]["next_steps"] = [_step("CACHE_RESULT", ["route_kyc"], 1.6), _step("CONDITIONAL_LLM_BYPASS", ["route_kyc"], 1.6, "review"),
                                       _step("CACHE_RESULT", ["route_checks"], 0.5)]
    first = _render(report)["first"]
    assert first.count('<li class="card">') == 2
    assert first.count("$1.60") == 1
    assert "Reuse a repeated result, or replace an LLM decision with a rule" in first
    assert 'data-first="2"' in first  # the second card still points at its own next step


def test_where_the_money_went_is_grouped_by_agent_with_the_agent_map_figures():
    report = _report()
    report["mine"]["by_agent"]["kyc"] = {"actions": [{"node": "kyc_lookup", "total_cost": 0, "occurrences": 4}]}
    rows = re.findall(r'<tr class="(agent|fold)">.*?</tr>', _render(report)["steps"])
    assert rows == ["agent", "agent", "fold"]
    steps = _render(report)["steps"]
    assert re.search(r'<th colspan="4" scope="rowgroup"><span class="step">sup</span> <span class="k">\$10\.00, 100% of spend', steps)
    assert "1 more step recorded no cost and no repeats." in steps


def test_handoffs_are_listed_in_run_order():
    rows = re.findall(r'<tr><td data-label="From">([^<]+)</td><td data-label="To">([^<]+)</td>', _render(_report())["handoffs"])
    assert rows == [("route_kyc", "credit"), ("route_kyc", "fraud"), ("route_kyc", "docs"), ("docs", "kyc"), ("route_checks", "letter")]
    assert "back to an earlier agent, 1 pass each" in _render(_report())["handoffs"]


def test_a_long_step_name_wraps_on_the_map_and_is_never_cut():
    events = []
    for run in ("r1", "r2"):
        events += [_ev(run, f"{run}a", "llm", cost=1.0), _ev(run, f"{run}b", "approve_transaction_after_review", f"{run}a")]
    report = {"availability": {"graph": {"available": True}}, "graph": build_graph(events, "wf", "invokes")["graph"],
              "summary": {"totals": {"cost": 2.0, "runs": 2}, "opportunities": [], "next_steps": []}, "mine": {"by_agent": {}}}
    page = _render(report)["map"]
    assert "…" not in page
    # the second line is in the title's type; what still overflows is squeezed to the box
    assert re.search(r'<text class="t"[^>]*>approve_</text><text class="t"[^>]*textLength[^>]*>transaction_after_review</text>', page)


def test_a_step_or_agent_with_no_recorded_cost_shows_a_dash_not_zero_dollars():
    report = _report()
    report["mine"]["by_agent"]["kyc"] = {"actions": [{"node": "kyc_lookup", "total_cost": 0, "occurrences": 4}]}
    page = _render(report)
    assert "$0<" not in page["steps"] and "$0," not in page["steps"]
    assert "kyc</span> <span class=\"k\">no cost recorded" in page["steps"]
    assert "$0" not in page["amPrint"] and "$0" not in page["amPanel"]


def test_a_decision_that_follows_from_its_input_is_a_lookup_card():
    report = _report()
    report["summary"]["next_steps"] = [{**_step("CONDITIONAL_LLM_BYPASS", ["route_checks"], 2.5, "replay"), "observations": 1242, "lookup_entries": 6}]
    first = _render(report)["first"]
    assert "Replace an LLM decision with a lookup" in first
    assert "6 different inputs, each always given the same choice, across 1,242 calls." in first


def test_the_page_gets_how_many_inputs_a_lookup_holds_not_the_inputs():
    from profiler_client.report import view
    lookup = {**_step("CONDITIONAL_LLM_BYPASS", ["route_checks"], 2.5), "by_input": [
        {"input": {"tier": "h:secret1"}, "choice": "reject", "observations": 40, "confirmed": True},
        {"input": {"tier": "h:secret2"}, "choice": "approve", "observations": 30, "confirmed": True},
        {"input": {"tier": "h:secret3"}, "choice": "approve", "observations": 2, "confirmed": False}]}
    page = view({"summary": {"opportunities": [lookup], "next_steps": [{**lookup, "lookup_entries": 2}]}})
    assert page["summary"]["opportunities"][0]["lookup_entries"] == 2
    assert page["summary"]["next_steps"][0]["lookup_entries"] == 2
    assert "secret" not in json.dumps(page)
