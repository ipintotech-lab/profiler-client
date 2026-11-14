"""The report page, run in Node with a stand-in for the page: every share of spend it shows for one
step is the same figure, worked out from cost and total cost, on the agent map, the hot spots, the
details panel and the steps table."""
import json
import re
import subprocess
from pathlib import Path

from test_graph import _agent_ev, _decision

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
