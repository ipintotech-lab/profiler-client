"""The overview's Agent flow tab (issue #59), run in Node with a stand-in page.

The fixture is a real result of the server's overview job over Israel's synthetic loan application
day (eight agents, a router, sixteen runs), prepared by this client with --keep-names. The diagram is
the graph builder's own layout; its priced remarks are exactly the counted findings, so they add up to
the workflow's headline and nothing is counted twice; "what if" takes exactly that off; the typical
run follows the busiest handoffs; and the share-safe view shows no name at all.
"""
import copy
import gzip
import json
import re
import subprocess
from pathlib import Path

import pytest

from profiler_client.report import view

JS = Path(__file__).parent.parent / "src" / "profiler_client" / "static" / "report.js"
FLOW = Path(__file__).parent / "data" / "agent-flow" / "report.json.gz"
SAVINGS = Path(__file__).parent / "data" / "savings-overview" / "report.json.gz"

# a page with no browser; ACTIONS (a list) clicks tabs, ticks boxes, plays the run and waits, then the slots are printed
HARNESS = """
const el = () => { const kids = new Map(); return { innerHTML: '', textContent: '', hidden: false, checked: false, dataset: {},
  style: {}, value: '', tabIndex: 0, classList: { toggle() {}, add() {}, remove() {} }, on: {}, attrs: {},
  addEventListener(t, f) { this.on[t] = f; }, setAttribute(k, v) { this.attrs[k] = String(v); }, kids,
  querySelector(sel) { if (!kids.has(sel)) kids.set(sel, el()); return kids.get(sel); },
  querySelectorAll: () => [], focus() { globalThis.focused = this; }, scrollIntoView() {}, childNodes: [], append() {} }; };
const text = e => [e.innerHTML || e.textContent, ...[...e.kids.values()].map(text)].join('');
const slots = new Map(), root = el(), tools = el();
root.querySelector = sel => { const k = (sel.match(/data-r="?([^"\\]]+)/) || [])[1];
  if (!k) return sel === '.tools' ? tools : null; if (!slots.has(k)) slots.set(k, el()); return slots.get(k); };
globalThis.window = {};
globalThis.CSS = { escape: x => x };
require(process.argv[1]);
const sleep = ms => new Promise(r => setTimeout(r, ms));
let input = ''; process.stdin.on('data', d => { input += d; }).on('end', async () => {
  window.ProfilerReport.render(root, JSON.parse(input), {});
  const tab = v => slots.get('tabs').querySelector(`[data-view="${v}"]`);
  for (const a of JSON.parse(process.env.ACTIONS || '[]')) {
    if (a.tab) slots.get('tabs').on.click({ target: { closest: () => ({ dataset: { view: a.tab } }) } });
    if (a.key) slots.get('tabs').on.keydown({ key: a.key, target: tab(a.from), preventDefault() {} });
    if (a.check) { const s = slots.get(a.check); s.checked = a.on !== false; s.on.change({ target: s }); }
    if (a.play) slots.get('flowPlay').on.click();
    if (a.wait) await sleep(a.wait);
  }
  const out = Object.fromEntries([...slots].map(([k, e]) => [k, { text: text(e), hidden: e.hidden, attrs: e.attrs }]));
  out.tabState = Object.fromEntries(['savings', 'flow'].map(v => [v, { selected: tab(v).attrs['aria-selected'], tabIndex: tab(v).tabIndex }]));
  console.log(JSON.stringify(out));
  process.exit(0);  // a typical run may still have a timer queued
});
"""


def _render(data: dict, actions: list | None = None) -> dict:
    out = subprocess.run(["node", "-e", HARNESS, str(JS)], input=json.dumps(data), capture_output=True, text=True,
                         check=True, encoding="utf-8", env={**__import__("os").environ, "ACTIONS": json.dumps(actions or [])})
    return json.loads(out.stdout)


def _load(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return view(json.load(f))


@pytest.fixture(scope="module")
def loan():
    return _load(FLOW)


@pytest.fixture(scope="module")
def flow(loan):
    return _render(loan, [{"tab": "flow"}])


def _plain(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s)


def _report(loan):
    return loan["reports"]["loan-application"]


# --- the diagram is the graph builder's ---------------------------------------------------------

def test_the_flow_draws_the_graph_builders_agents_decisions_and_handoffs(loan, flow):
    ag = _report(loan)["graph"]["agent_graph"]
    base = flow["flowBase"]["text"]
    assert base.count('class="fl-agent') == len(ag["agents"]) - 1  # the router is the bar across the top
    assert base.count('class="fl-dec') == len(ag["decisions"]) == 5
    assert base.count('class="fl-hand"') == sum(1 for link in ag["links"] if link["kind"] == "handoff") == 9
    assert base.count('class="fl-back"') == 1 and 'class="fl-frame"' in base and "start together" in base
    assert "intake-supervisor · router" in _plain(base) and "43% of spend" in _plain(base)
    assert 'viewBox' in str(flow["flowSvg"]["attrs"]) and flow["flowSvg"]["attrs"]["viewBox"] == "0 0 920 440"


def test_a_dataset_whose_workflows_each_have_one_agent_says_there_is_no_flow():
    out = _render(_load(SAVINGS), [{"tab": "flow"}])
    assert not out["flowNone"]["hidden"] and "each workflow in this dataset has one agent" in out["flowNone"]["text"]
    assert out["flowBody"]["hidden"]


# --- remarks: priced ones are the counted findings, once each -------------------------------------

def _priced(flow):
    return [float(x) for x in re.findall(r'<span class="fl-sv">\$([0-9.]+)</span>', flow["flowRemarks"]["text"])]


def test_the_priced_remarks_add_up_to_the_workflows_headline(loan, flow):
    saved = _report(loan)["summary"]["replaceable"]["cost"]
    counted = sum(f["saving"] for f in _report(loan)["summary"]["findings"] if f.get("counted"))
    assert counted == pytest.approx(saved)
    assert sum(_priced(flow)) == pytest.approx(saved, abs=0.002)  # each shown to the tenth of a cent
    assert "$0.164 (30%) could be saved" in _plain(flow["flowSum"]["text"]) and "All review first" in flow["flowSum"]["text"]


def test_remarks_that_are_not_counted_say_so_and_carry_no_price(flow):
    items = flow["flowRemarks"]["text"].split("<li ")[1:]
    plain = [i for i in items if "fl-sv" not in i]
    assert plain and all(any(n in i for n in ("Not priced yet", "Adds nothing to the total", "For information")) for i in plain)
    text = _plain(flow["flowRemarks"]["text"])
    assert "Script a fixed sequence" in text and "already counted above" in text  # the chain, in no sum
    assert "both call" in text and "get_transaction_history" in text  # the duplicate fetch, not priced
    assert "Rare but costly" in text and "compliance-review" in text
    assert "A loop back" in text and "document-collection" in text


def test_a_findings_own_agent_places_its_saving(loan):
    # until the server names each finding's agent, savings on steps several agents run are listed together;
    # with the agent named, every dollar lands on its agent and the shared remark goes
    data = copy.deepcopy(loan)
    findings = _report(data)["summary"]["findings"]
    before = _render(data, [{"tab": "flow"}])
    assert "Counted on steps several agents share" in before["flowRemarks"]["text"]
    for f in findings:
        if f.get("counted") and f["title"] in ("llm_call ← route_after_kyc", "llm_call ← get_account_balance"):
            f["agent"] = "credit-assessment"
        elif f.get("counted") and f["title"] in ("llm_call ← place_hold",):
            f["agent"] = "fraud-screening"
        elif f.get("counted") and f["title"] in ("llm_call ← route_after_checks", "llm_call ← get_rate_table"):
            f["agent"] = "pricing"
    after = _render(data, [{"tab": "flow"}])
    assert "Counted on steps several agents share" not in after["flowRemarks"]["text"]
    assert sum(_priced(after)) == pytest.approx(sum(_priced(before)), abs=0.002)  # moved, never added


# --- what if, the typical run, traffic ----------------------------------------------------------

def test_what_if_takes_off_exactly_the_counted_savings(loan):
    out = _render(loan, [{"tab": "flow"}, {"check": "flowWhatIf"}, {"wait": 1100}])
    s = _report(loan)["summary"]
    total, saved = s["totals"]["cost"], s["replaceable"]["cost"]
    assert _plain(out["flowSum"]["text"]).startswith(f"${total - saved:.3f} after the counted remarks")
    assert "−30%" in out["flowSum"]["text"] and 'class="fl-saved"' in out["flowBase"]["text"]
    back = _render(loan, [{"tab": "flow"}, {"check": "flowWhatIf"}, {"wait": 1100}, {"check": "flowWhatIf", "on": False}, {"wait": 1100}])
    assert 'class="fl-saved"' not in back["flowBase"]["text"] and "$0.164 (30%) could be saved" in _plain(back["flowSum"]["text"])


def test_the_typical_run_follows_the_busiest_handoffs_and_says_it_is_not_recorded(loan):
    out = _render(loan, [{"tab": "flow"}, {"play": True}, {"wait": 12500}])
    run = out["flowRun"]["text"]
    assert run.startswith("A typical run: 10 steps") and "not a recorded run" in run


def test_traffic_dots_follow_each_handoff_by_its_share_and_can_be_turned_off(loan, flow):
    links = _report(loan)["graph"]["agent_graph"]["links"]
    expected = sum(max(1, round(link["share"] * 4)) for link in links if link["kind"] == "handoff") + \
        sum(1 for link in links if link["kind"] == "direct" and link.get("mode") == "return")
    assert flow["flowTrafficLayer"]["text"].count("<animateMotion") == expected
    off = _render(loan, [{"tab": "flow"}, {"check": "flowTraffic", "on": False}])
    assert off["flowTrafficLayer"]["text"] == ""


# --- share-safe: nothing anyone could match ----------------------------------------------------

def test_share_safe_shows_neutral_labels_and_no_name(loan):
    out = _render(loan, [{"tab": "flow"}, {"check": "flowSafe"}])
    shown = _plain(out["flowBase"]["text"] + out["flowRemarks"]["text"])
    ag = _report(loan)["graph"]["agent_graph"]
    for name in [a["id"] for a in ag["agents"]] + [d["id"] for d in ag["decisions"]] + ["get_customer_profile", "get_transaction_history"]:
        assert name not in shown, name
    assert "Router" in shown and "Agent A" in shown and "Decision 1" in shown and "Step 1" in shown
    assert not re.search(r"h:[0-9a-f]{7}", shown)  # no pseudonym either


# --- the tabs ------------------------------------------------------------------------------------

def test_tabs_switch_views_by_click_and_by_arrow_keys(loan):
    first = _render(loan)
    assert not first["viewSavings"]["hidden"] and first["viewFlow"]["hidden"]
    out = _render(loan, [{"key": "ArrowRight", "from": "savings"}])
    assert out["viewSavings"]["hidden"] and not out["viewFlow"]["hidden"]
    assert out["tabState"] == {"savings": {"selected": "false", "tabIndex": -1}, "flow": {"selected": "true", "tabIndex": 0}}
    home = _render(loan, [{"tab": "flow"}, {"key": "Home", "from": "flow"}])
    assert not home["viewSavings"]["hidden"] and home["tabState"]["savings"]["selected"] == "true"
