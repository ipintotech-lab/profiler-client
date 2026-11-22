"""The report page, run in Node with a stand-in for the page: every share of spend it shows for one
step is the same figure, worked out from cost and total cost, on the agent map, the hot spots, the
details panel and the steps table."""
import json
import os
import re
import subprocess
from pathlib import Path

from test_graph import _agent_ev, _decision, _ev

from profiler_client.contract import TraceEvent
from profiler_client.graph import build_graph

JS = Path(__file__).parent.parent / "src" / "profiler_client" / "static" / "report.js"

# a page with no browser: each [data-r] slot keeps what the report writes into it
HARNESS = """
const el = () => { const kids = new Map(); return { innerHTML: '', textContent: '', hidden: false, dataset: {}, style: {},
  classList: { toggle() {}, add() {}, remove() {} }, on: {}, addEventListener(t, f) { this.on[t] = f; }, setAttribute() {}, kids,
  querySelector: sel => { if (!kids.has(sel)) kids.set(sel, el()); return kids.get(sel); },
  querySelectorAll: () => [], focus() {}, scrollIntoView() {}, childNodes: [], append() {} }; };
const text = e => [e.innerHTML || e.textContent, ...[...e.kids.values()].map(text)].join('');
const slots = new Map(), root = el(), tools = el();  // .tools: where the host's buttons go
root.querySelector = sel => { const k = (sel.match(/data-r="?([^"\\]]+)/) || [])[1];
  if (!k) return sel === '.tools' ? tools : null; if (!slots.has(k)) slots.set(k, el()); return slots.get(k); };
globalThis.window = {};
globalThis.CSS = { escape: x => x };
require(process.argv[1]);
let input = ''; process.stdin.on('data', d => { input += d; }).on('end', () => {
  // a host that takes declarations; "declare <workflow>" opens that workflow and clicks the first button
  const declared = [], declareReadOnly = process.argv[2] === 'declare' ? (...args) => declared.push(args) : null;
  window.ProfilerReport.render(root, JSON.parse(input), { name: 'wf', declareReadOnly, workflow: process.argv[3] || null });
  if (process.argv[3]) slots.get('all').on.click({ target: { closest: () => ({ dataset: { declare: '0' } }) } });
  // CLICK: a button's dataset to click in All findings, after the first draw
  if (process.env.CLICK) slots.get('all').on.click({ target: { closest: () => ({ dataset: JSON.parse(process.env.CLICK) }) } });
  console.log(JSON.stringify({ ...Object.fromEntries([...slots].map(([k, e]) => [k, text(e)])), declared }));
});
"""


def _render(report: dict, *host: str, click: dict | None = None) -> dict:
    env = {**os.environ, "CLICK": json.dumps(click)} if click else None
    out = subprocess.run(["node", "-e", HARNESS, str(JS), *host], input=json.dumps(report), capture_output=True,
                         text=True, check=True, env=env, encoding="utf-8")
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


def test_an_agents_llm_turns_are_one_card_and_another_agents_turn_of_the_same_name_its_own():
    report = _report()
    turns = [{"title": "llm_call ← user_prompt", "saving": 2.6, "observations": 1214, "evidence": ""},
             {"title": "llm_call ← send_letter", "saving": 2.5, "observations": 1214, "evidence": ""}]
    sup = {**_step("CONDITIONAL_LLM_BYPASS", ["llm_call ← user_prompt"], 5.1, "review"), "agent": "sup", "turns": turns}
    kyc = {**_step("CONDITIONAL_LLM_BYPASS", ["llm_call ← user_prompt"], 0.4, "review"), "agent": "kyc", "turns": turns[:1]}
    report["summary"]["next_steps"] = [sup, kyc]
    first = _render(report)["first"]
    assert first.count('<li class="card">') == 2
    assert "Replace an LLM decision with a rule: 2 LLM turns of" in first
    assert first.count("$5.10") == 1 and "$2.60" in first and "$2.50" in first
    assert "1 LLM turn of" in first


LABELS = {"low": "Low observational risk — not replay-verified", "medium": "Needs review", "high": "Investigate first"}
RISKS = {"low": {"count": 2, "counted": 2, "saving": 3.5}, "medium": {"count": 5, "counted": 0, "saving": 0},
         "high": {"count": 1, "counted": 0, "saving": 0}}


def test_three_risk_tiles_under_the_headline_with_each_risk_named_in_text():
    report = _report()
    report["summary"].update(replaceable={"cost": 3.5, "findings": 2}, risk_totals=RISKS, risk_labels=LABELS)
    tiles = _render(report)["risks"]
    for text in ("Low risk", LABELS["low"], "$3.50", "2 findings<", "Replay it against past calls",
                 "Needs review", "No saving counted", "5 findings, none in the headline",
                 "Investigate first", "1 finding, none in the headline"):
        assert text in tiles, text
    # Investigate first is counted, not priced
    assert tiles.count("No saving counted") == 1 and tiles.index("Investigate first") > tiles.index("No saving counted")


def test_the_overview_has_the_datasets_risk_tiles():
    report = _report()
    data = {"overview": {"workflows": [{"name": "wf", "runs": 3, "spend": 10.0, "replaceable": 3.5, "findings": 2,
                                        "highest_risk": "high"}],
                         "totals": {"spend": 10.0, "replaceable": 3.5, "share": 0.35, "findings": 2},
                         "risk_totals": RISKS, "risk_labels": LABELS},
            "reports": {"wf": report}}
    tiles = _render(data)["risks"]
    assert "$3.50" in tiles and "Investigate first" in tiles


def test_a_report_without_risk_totals_has_no_tiles():
    # reports made before the server sent risk_totals, and diagnose mode
    assert _render(_report()).get("risks", "") == ""


def test_a_tile_says_how_many_of_its_findings_the_headline_counts():
    report = _report()
    risks = {**RISKS, "medium": {"count": 91, "counted": 13, "saving": 6.97}}
    report["summary"].update(risk_totals=risks, risk_labels=LABELS)
    assert "91 findings, 13 in the headline" in _render(report)["risks"]
    # a result from before the counted figure: the count alone
    report["summary"]["risk_totals"] = {r: {"count": t["count"], "saving": t["saving"]} for r, t in risks.items()}
    assert "91 findings<" in _render(report)["risks"]


def _finding(title, saving, risk="low", counted=True, held_out=None, observations=12, covered_by=None):
    return {"type": "CACHE_RESULT", "title": title, "nodes": [title], "saving": saving, "observations": observations,
            "risk": risk, "risk_reasons": [], "counted": counted, "held_out": held_out, "covered_by": covered_by}


def test_all_findings_lists_counted_findings_first_and_says_why_others_are_held_out():
    report = _report()
    report["summary"].update(risk_labels=LABELS, findings=[
        _finding("fetch_rates", 2.0, held_out="declaration", counted=False),
        _finding("kyc_lookup", 1.5),
        _finding("lookup_again", 0.5, "medium", counted=False, held_out="overlap"),
    ])
    table = _render(report)["all"]
    assert table.index("kyc_lookup") < table.index("fetch_rates") < table.index("lookup_again")
    assert table.count('class="held"') == 2 and "$1.50" in table and "$2.00" in table
    assert "Does this step only read?</b> Not in the headline until you say so: declare it read-only" in table
    assert "a larger finding already counts" in table and "Needs review" in table
    # a host that takes declarations gets a button instead
    hosted = _render(report, "declare")["all"]
    assert 'data-declare="0"' in hosted and "until you say so" not in hosted


def test_a_workflow_opened_from_the_overview_hands_its_declarations_to_the_host():
    report = _report()
    report["observe"] = {"selected_workflow": "wf"}
    report["summary"].update(risk_labels=LABELS, findings=[_finding("fetch_rates", 2.0, held_out="declaration", counted=False)])
    overview = {"overview": {"workflows": [{"name": "wf", "runs": 3, "spend": 10.0, "replaceable": 0, "findings": 0}],
                             "totals": {"spend": 10.0, "replaceable": 0, "findings": 0}},
                "reports": {"wf": report}}
    out = _render(overview, "declare", "wf")
    assert 'data-declare="0"' in out["all"] and "until you say so" not in out["all"]
    assert out["declared"] == [[["fetch_rates"], "wf"]]  # the steps, and which workflow they belong to


def test_all_findings_shows_ten_then_a_show_all_button_and_is_absent_from_older_results():
    report = _report()
    report["summary"].update(risk_labels=LABELS, findings=[_finding(f"step_{i}", 1.0 + i) for i in range(12)])
    table = _render(report)["all"]
    assert table.count("<tr") == 11 and "Show all 12 findings" in table
    del report["summary"]["findings"]
    assert _render(report).get("all", "") == ""


def _covered() -> dict:
    report = _report()
    report["summary"].update(risk_labels=LABELS, findings=[
        _finding("route_bypass", 0.9),
        _finding("cache_a", 0.2, counted=False, held_out="declaration", covered_by=0),
        _finding("cache_b", 0.1, counted=False, held_out="overlap", covered_by=0),
        _finding("fetch_rates", 0.3, counted=False, held_out="declaration"),
        _finding("no_gain", None, counted=False, held_out="no_saving"),
    ])
    return report


def test_findings_a_counted_one_covers_fold_under_it_behind_one_line():
    table = _render(_covered(), "declare")["all"]
    rows = re.findall(r"<tr[^>]*>.*?</tr>", table)
    assert len(rows) == 4  # the parent, its fold line, and the two held rows nothing covers
    assert "route_bypass" in rows[0] and 'class="fold"' in rows[1] and "fetch_rates" in rows[2] and "no_gain" in rows[3]
    assert '+2 smaller findings on the same calls' in rows[1] and 'aria-expanded="false"' in rows[1]
    assert "cache_a" not in table and "cache_b" not in table
    assert 'data-declare="3"' in rows[2]  # an uncovered held row keeps its question


def test_the_fold_line_opens_the_covered_findings_under_their_parent():
    table = _render(_covered(), "declare", click={"covered": "0"})["all"]
    rows = re.findall(r"<tr[^>]*>.*?</tr>", table)
    assert [r for r in rows if 'class="held sub"' in r] == rows[2:4]
    assert "cache_a" in rows[2] and "cache_b" in rows[3] and "Hide the 2 smaller findings" in rows[1]
    # a covered finding isn't asked about: declaring it would change nothing, the parent already counts its calls
    assert 'data-declare="1"' not in table and rows[2].count("a larger finding already counts these calls") == 1


def test_rows_without_covered_by_are_listed_as_before():
    report = _covered()
    for r in report["summary"]["findings"]:
        del r["covered_by"]
    table = _render(report)["all"]
    assert 'class="fold"' not in table and table.count('class="held"') == 4


def _capacity(report: dict, by_agent=None) -> dict:
    report["profile"] = {"x": {"x": 4.0}, "y": {"y": 4.0}, "by_agent": by_agent, "sensitivity": [
        {"ratio": 1.0, "x": {"x": -0.0}}, {"ratio": 1.5, "x": {"x": 4.0}}, {"ratio": 2.0, "x": {"x": 4.0}}]}
    report["formula"] = {"used": {"demand_model": "p95", "unit_cost": 0.01, "unit_value": 0.02, "capacity_headroom": 2.0},
                         "defaults": {"usage_stats": {"count": 30, "p50": 3.0, "max": 6.0}, "provenance": {
                             "unit_cost": {"label": "DERIVED", "note": "x"}, "unit_value": {"label": "ASSUMED", "note": "x"},
                             "capacity_headroom": {"label": "ASSUMED", "note": "x"}, "max_demand": {"label": "DERIVED", "note": "x"}}}}
    return report


def test_each_workflow_has_a_capacity_view_from_its_own_pipeline_result():
    body = _render(_capacity(_report()))["capBody"]
    assert "<b>4 calls</b> per run" in body and "p95 of 30 runs (median 3 calls, most 6 calls)" in body
    assert "doesn’t change any finding" in body


def test_the_capacity_inputs_say_in_plain_words_which_come_from_the_traces():
    body = _render(_capacity(_report()))["capBody"]
    assert '<td>Cost of a call</td><td data-label="Source">From your traces</td>' in body
    assert '<td>Value of a used call</td><td data-label="Source">Assumed</td><td data-label="How">Traces can’t show it, so it is set to 2× the cost of a call.' in body
    assert "At most 2× the peak" in body and "p95 of calls per run across 30 runs" in body
    assert "unit_cost" not in body and "--" not in body  # the server's notes for developers stay out


def test_capacity_sizes_that_repeat_are_merged_into_one_row():
    body = _render(_capacity(_report()))["capBody"]
    assert "1× its cost</td><td class=\"num\" data-label=\"Size for\">None, not worth providing" in body  # the solve's -0
    assert "1.5× to 2× its cost</td><td class=\"num\" data-label=\"Size for\">4 calls" in body and body.count("its cost</td>") == 2


def test_the_capacity_view_splits_a_multi_agent_workflow_by_agent():
    by_agent = {"mode": "multi_agent", "agents": [{"agent": "kyc", "demand": 3.0, "x": 3.0},
                                                  {"agent": "sup", "demand": 5.0, "x": 5.0}]}
    body = _render(_capacity(_report(), by_agent))["capBody"]
    assert "Peak calls per run" in body and "kyc" in body and "5 calls" in body


def test_a_result_without_a_capacity_solve_has_no_capacity_view():
    assert _render(_report())["capBody"] == ""


def _linked_report(parent_by):
    def llm(run, eid, trigger):
        return TraceEvent(trace_id="t", run_id=run, workflow="wf", agent="a", event_type="decision", model="m",
                          node=f"llm_call ← {trigger}", event_id=eid, trigger=trigger, cost=1.0)
    events = []
    for run in ("r1", "r2"):
        tool = _ev(run, f"{run}t", "search", f"{run}l1").model_copy(update={"parent_by": parent_by})
        events += [llm(run, f"{run}l1", "user_prompt"), tool, llm(run, f"{run}l2", "search")]
    return {"availability": {"graph": {"available": True}}, "graph": build_graph(events, "wf", "invokes")["graph"],
            "summary": {"totals": {"cost": 4.0, "runs": 2}, "opportunities": [], "next_steps": []}, "mine": {"by_agent": {}}}


def test_a_link_from_step_order_is_dashed_ran_next_with_the_step_order_line_and_never_chose():
    page = _render(_linked_report("order"))
    assert 'class="edge k-follows' in page["map"] and "(ran next)" in page["map"]
    assert "ran next: taken from the order steps ran in" in page["map"] and "read the result of: " in page["map"]
    assert "Built from step order within each run. Steps that ran in parallel may appear in sequence." in page["map"]
    assert "chose" not in page["map"]


def test_a_recorded_link_is_chose_and_the_map_has_no_step_order_line():
    page = _render(_linked_report(None))["map"]
    assert "(chose)" in page and "chose: the trace records" in page
    assert "ran next" not in page and "Built from step order" not in page


def _needs_review(**extra):
    return {**_finding("route_checks", None, "medium", counted=False, held_out="needs_review", observations=1223),
            "type": "PARTIAL_LLM_BYPASS", "consistency": 0.8545, "covered_cost": 0.397, **extra}


def test_a_rule_for_most_calls_needs_review_with_its_consistency_and_the_cost_it_covers():
    report = _report()
    report["summary"].update(risk_labels=LABELS, findings=[_finding("kyc_lookup", 1.5), _needs_review()])
    report["summary"]["next_steps"] = [{**_step("PARTIAL_LLM_BYPASS", ["route_checks"], None, "review"),
                                        "observations": 1223, "consistency": 0.8545, "covered_cost": 0.397}]
    report["summary"]["opportunities"] = [{"type": "PARTIAL_LLM_BYPASS", "nodes": ["route_checks"], "title": "route_checks",
                                           "saving": None, "observations": 1223}]
    page = _render(report)
    evidence = "A rule handles 85% of 1,223 calls, the rest falls back to the model. The calls the rule handles cost $0.397."
    for slot in ("all", "first"):
        assert "Needs review: a rule for most calls" in page[slot] and evidence in page[slot]
    assert "Not in the headline: needs review" in page["all"] and page["all"].count('class="held"') == 1
    assert "No saving counted" in page["first"]


def test_the_step_map_marks_a_needs_review_decision_in_words():
    events = []
    for run in ("r1", "r2", "r3"):
        events += [_ev(run, f"{run}a", "lookup"),
                   TraceEvent(trace_id="t", run_id=run, workflow="wf", agent="a", event_type="decision", node="route",
                              event_id=f"{run}d", parent=f"{run}a", model="m", cost=0.01),
                   _ev(run, f"{run}b", "send", f"{run}d")]
    report = {"availability": {"graph": {"available": True}}, "graph": build_graph(events, "wf", "invokes")["graph"],
              "summary": {"totals": {"cost": 0.03, "runs": 3}, "next_steps": [],
                          "opportunities": [{"type": "PARTIAL_LLM_BYPASS", "nodes": ["route"], "title": "route",
                                             "saving": None, "observations": 3}]}}
    page = _render(report)["map"]
    assert 'data-marks="needs review"' in page and "LLM call a rule could handle most of the time" in page


def test_the_page_keeps_a_needs_review_findings_consistency_and_covered_cost():
    from profiler_client.report import view
    o = {"type": "PARTIAL_LLM_BYPASS", "title": "route", "nodes": ["route"], "saving": None, "observations": 20,
         "consistency": 0.85, "covered_cost": 0.17}
    page = view({"summary": {"opportunities": [o], "next_steps": [{**o, "step": "review"}]}})
    for kept in (page["summary"]["opportunities"][0], page["summary"]["next_steps"][0]):
        assert kept["consistency"] == 0.85 and kept["covered_cost"] == 0.17


_ESTIMATED = {"gaps": [{"key": "cost", "label": "Cost", "status": "estimated", "coverage": 0, "effect": "estimated"}]}


def test_an_estimated_cost_is_marked_under_the_headline():
    report = _report()
    report["summary"] = {**report.get("summary", {}), "replaceable": {"cost": 3.5, "findings": 2},
                         "totals": {"cost": 10.0}}
    assert "estimated" not in _render(report)["sub"]
    sub = _render({**report, "data_quality": _ESTIMATED})["sub"]
    assert "$3.50 of $10.00" in sub
    assert '<span class="estimated">Cost estimated from tokens at your prices' in sub


def test_the_overview_is_marked_when_any_workflows_cost_is_estimated():
    overview = {"overview": {"workflows": [{"name": "wf", "runs": 3, "spend": 10.0, "replaceable": 3.5, "findings": 2}],
                             "totals": {"spend": 10.0, "replaceable": 3.5, "findings": 2}},
                "reports": {"wf": {**_report(), "data_quality": _ESTIMATED}}}
    assert "Cost estimated from tokens" in _render(overview)["sub"]
