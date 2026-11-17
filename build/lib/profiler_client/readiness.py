"""How much the analysis can find in a dataset, before it is uploaded.

The attribute checks and the grade are the profiler server's own: the
server imports them from here, so the grade ``profiler-client check``
shows on this machine is the grade the server gives after upload. Every
threshold quoted here is defined here, once.

Trace formats differ in what they record: a Claude Code transcript has
tokens, cost, timing, real tool inputs and output digests; a public
benchmark dump may have no tokens or cost at all, and an anonymized one
only placeholder inputs. Missing attributes don't break the analysis --
they silently turn into zeros -- so the check says up front which ones
are missing and which findings that rules out.

Everything returned is counts, shares, keys and thresholds: no name, id
or value from a trace.
"""
from __future__ import annotations

from collections import Counter, defaultdict

# Below this share of events having an attribute, it counts as partial.
COMPLETE_SHARE = 0.9

# A rule finding (an LLM choice a rule could make) needs a step seen at least this often.
MIN_BYPASS_OBSERVATIONS = 10

# Duplicate work between agents is reported once it shows in at least this many runs (the server's patterns.MIN_SHARED_RUNS).
MIN_SHARED_RUNS = 10

# Below this many events, fewer rules are found (docs/e2e-synthetic/2026-11-13 in the server repo).
SMALL_DATASET_EVENTS = 5000

# (key, what it is, which events need it, how to tell it's there, what is empty without it)
_ATTRIBUTES = [
    (
        "tokens",
        "LLM token counts",
        "llm",
        lambda e: e.tokens.input + e.tokens.output > 0,
        "Token totals show 0 and cost can't be estimated from tokens.",
    ),
    (
        "cost",
        "cost ($)",
        "llm",
        lambda e: e.cost > 0,
        "Savings show $0, so opportunities are ranked by how often they repeat instead of by money.",
    ),
    (
        "llm_latency",
        "LLM call latency",
        "llm",
        lambda e: e.latency_ms > 0,
        "Time spent waiting on the model can't be measured.",
    ),
    (
        "tool_latency",
        "tool call latency",
        "tool",
        lambda e: e.latency_ms > 0,
        "Time spent in tools can't be measured.",
    ),
    (
        "tool_input",
        "real tool inputs",
        "tool",
        lambda e: bool(e.input_state) and "anonymized" not in e.input_state,
        "Repeated calls with the same input can't be recognized, so caching and rule opportunities are under-counted.",
    ),
    (
        "tool_output",
        "tool outputs",
        "tool",
        lambda e: bool(e.result) and e.result != "no_result",
        "Whether the same input gives the same output can't be checked, so determinism and caching can't be judged.",
    ),
    (
        "user_input",
        "the user's request",
        "all",
        lambda e: bool(e.user_input),
        "The report can't show which request started each run.",
    ),
]


def is_llm(e) -> bool:
    return bool(e.model) or e.node.startswith("llm_call")


def is_tool(e) -> bool:
    return e.tool is not None or e.event_type == "tool_call"


def check_data_quality(events: list, estimated: dict | None = None) -> dict:
    """Coverage of each attribute the report relies on, and the gaps.

    ``events`` are trace events with the contract's fields as attributes.
    ``estimated`` (from the server's ``estimate_missing_costs``) marks cost
    as filled in by an estimate rather than recorded."""
    groups = {"llm": [e for e in events if is_llm(e)], "tool": [e for e in events if is_tool(e)], "all": events}
    attributes, gaps = [], []
    for key, label, group, present, effect in _ATTRIBUTES:
        pool = groups[group]
        if not pool:
            continue
        share = sum(1 for e in pool if present(e)) / len(pool)
        status = "ok" if share >= COMPLETE_SHARE else "partial" if share > 0 else "missing"
        entry = {"key": key, "label": label, "coverage": round(share, 3), "status": status}
        if key == "cost" and estimated:
            entry["status"] = "estimated"
            entry["effect"] = (
                f"Cost is estimated for {estimated['events']} LLM calls from their tokens "
                f"(${estimated['input_per_mtok']:g} / ${estimated['output_per_mtok']:g} per 1M input / output tokens). "
                "Every input token is priced at the full rate (cached input usually costs less), so treat it as an upper bound."
            )
            gaps.append(entry)
        elif status != "ok":
            entry["effect"] = effect
            gaps.append(entry)
        attributes.append(entry)
    keys = {g["key"]: g["status"] for g in gaps}
    llm_tokens_ok = "tokens" not in keys and groups["llm"]
    return {
        "complete": not gaps,
        "attributes": attributes,
        "gaps": gaps,
        # cost is missing but tokens are there: the user can opt in to an estimate
        "can_estimate_cost": bool(keys.get("cost") in ("missing", "partial") and llm_tokens_ok),
    }


# The grade: what each part is worth, out of 100. The user's request
# isn't graded -- profiler-client drops it on purpose.
GRADE_WEIGHTS = {"runs": 30, "money": 25, "tool_input": 20, "tool_output": 15, "timing": 10}
GOOD_RUNS, FEW_RUNS = 20, 5
GRADES = ((85, "A", "Good"), (70, "B", "Usable"), (50, "C", "Thin"), (0, "D", "Too thin"))


def grade_dataset(events: list, runs: int) -> dict:
    """A letter grade for how much the report can find in these events,
    before it runs, and what each check found. ``runs`` -- the runs of the
    workflow the report analyzes. A check with nothing to look at (no tool
    calls) is left out, and the others make up the score."""
    quality = check_data_quality(events)
    status = {a["key"]: a["status"] for a in quality["attributes"]}
    can_estimate = quality["can_estimate_cost"]
    checks = []

    def add(key, label, state, detail):
        checks.append({"key": key, "label": label, "status": state, "detail": detail})

    add("runs", "Runs of the same task",
        "ok" if runs >= GOOD_RUNS else "partial" if runs >= FEW_RUNS else "missing",
        f"{runs} run{'' if runs == 1 else 's'}. Repeats show up across runs: "
        f"{GOOD_RUNS} or more is good, under {FEW_RUNS} finds little.")
    money = status.get("cost")
    if money is not None:
        if money != "ok" and can_estimate:
            add("money", "Cost", "partial", "Cost isn't recorded, but tokens are: give token prices to estimate it.")
        else:
            add("money", "Cost", money, "Recorded for every LLM call." if money == "ok" else
                "Not recorded, so savings show $0 and repeats are ranked by count.")
    for key, label, ok_text, gap_text in (
        ("tool_input", "Tool inputs", "Repeated calls with the same input can be recognized.",
         "Repeated calls can't be matched by input, so caching is under-counted."),
        ("tool_output", "Tool outputs", "Whether the same input gives the same result can be checked.",
         "Whether a step gives the same result can't be checked."),
    ):
        if key in status:
            add(key, label, status[key], ok_text if status[key] == "ok" else gap_text)
    timing = [status[k] for k in ("llm_latency", "tool_latency") if k in status]
    if timing:
        worst = "missing" if all(t == "missing" for t in timing) else "ok" if all(t == "ok" for t in timing) else "partial"
        add("timing", "Timing", worst, "Recorded." if worst == "ok" else "Partly or not recorded, so time spent can't be measured.")

    value = {"ok": 1.0, "partial": 0.5, "missing": 0.0}
    total = sum(GRADE_WEIGHTS[c["key"]] for c in checks)
    score = round(100 * sum(GRADE_WEIGHTS[c["key"]] * value[c["status"]] for c in checks) / total)
    letter, word = next((g, w) for floor, g, w in GRADES if score >= floor)
    return {"grade": letter, "word": word, "score": score, "checks": checks, "can_estimate_cost": can_estimate}


def workflows(events: list) -> list[dict]:
    """Each workflow's events and runs, largest first: the server's order,
    whose first workflow is the one a report analyzes by default."""
    counts: Counter = Counter()
    runs: dict[str, set] = defaultdict(set)
    for e in events:
        counts[e.workflow] += 1
        runs[e.workflow].add(e.run_id)
    return [{"name": name, "events": count, "runs": len(runs[name])}
            for name, count in sorted(counts.items(), key=lambda kv: -kv[1])]


def _share_status(statuses: list[str | None]) -> str:
    if any(s in (None, "missing") for s in statuses):
        return "no"
    return "yes" if all(s == "ok" for s in statuses) else "limited"


def _runs_with_agents(events: list) -> int:
    """How many runs hold tool calls by more than one agent."""
    agents: dict[str, set] = defaultdict(set)
    for e in events:
        if is_tool(e) and e.agent:
            agents[e.run_id].add(e.agent)
    return sum(len(a) > 1 for a in agents.values())


def findings_possible(events: list, runs: int, prices: bool = False) -> list[dict]:
    """For each kind of finding: ``yes``, ``limited`` or ``no`` that the
    analysis can make it from these events (one workflow's), and why."""
    status = {a["key"]: a for a in check_data_quality(events)["attributes"]}

    def coverage(key):
        a = status.get(key)
        return "not recorded (no tool calls)" if a is None else f"{a['coverage']:.0%} of calls"

    tools = [status.get("tool_input", {}).get("status"), status.get("tool_output", {}).get("status")]
    tools_state = _share_status(tools)
    tools_reason = (f"tool arguments on {coverage('tool_input')}, results on {coverage('tool_output')}")

    cost = status.get("cost", {}).get("status")
    tokens = status.get("tokens", {}).get("status")
    priced = cost == "ok" or (prices and tokens == "ok")
    if cost == "ok":
        money = ("yes", "cost recorded on every LLM call")
    elif prices and tokens in ("ok", "partial"):
        money = ("yes" if tokens == "ok" else "limited", f"estimated from tokens ({coverage('tokens')}) at your prices")
    elif cost == "partial":
        money = ("limited", f"cost recorded on {coverage('cost')}")
    elif tokens in ("ok", "partial"):
        money = ("limited", "cost not recorded; pass --prices to estimate it from tokens")
    else:
        money = ("no", "neither cost nor tokens recorded")

    per_step = Counter(e.node for e in events if is_llm(e))
    steps = sum(1 for n in per_step.values() if n >= MIN_BYPASS_OBSERVATIONS)
    if not steps:
        rule = ("no", f"no LLM step is seen {MIN_BYPASS_OBSERVATIONS} or more times")
    elif not priced:
        rule = ("no", "a rule finding needs a cost above zero; cost not recorded"
                + ("; pass --prices to estimate it" if tokens in ("ok", "partial") else ""))
    else:
        rule = ("yes", f"{steps} LLM step{'' if steps == 1 else 's'} seen {MIN_BYPASS_OBSERVATIONS} or more times")

    sequence = ("yes" if runs >= GOOD_RUNS else "limited" if runs >= FEW_RUNS else "no",
                f"{runs} run{'' if runs == 1 else 's'}; {GOOD_RUNS} or more is good, under {FEW_RUNS} finds little")

    linked = sum(1 for e in events if e.parent)
    links = ("yes", f"parent links on {linked / len(events):.0%} of events") if linked else \
        ("no", "no parent links recorded")

    # the same tool call by more than one agent in a run: tool inputs, agent names and run ids
    tool_agents = {e.agent for e in events if is_tool(e) and e.agent}
    missing = [what for what, gone in (
        ("tool arguments", tools[0] in (None, "missing")),
        ("agent names on tool calls", not tool_agents),
        (f"{MIN_SHARED_RUNS} or more runs", runs < MIN_SHARED_RUNS)) if gone]
    if missing:
        between = ("no", "needs " + ", ".join(missing))
    elif len(tool_agents) < 2:
        between = ("no", "only one agent calls tools")
    elif not _runs_with_agents(events):
        between = ("no", "no run id holds tool calls by more than one agent; give a whole run one run id")
    else:
        between = ("yes" if tools[0] == "ok" else "limited",
                   f"{len(tool_agents)} agents call tools; tool arguments on {coverage('tool_input')}")

    rows = [
        ("reuse_result", "Reuse a repeated result", (tools_state, tools_reason)),
        ("rule_for_llm", "Replace an LLM decision with a rule", rule),
        ("script_sequence", "Script a fixed sequence", sequence),
        ("unstable_result", "Unstable result", (tools_state, tools_reason)),
        ("between_agents", "Duplicate work between agents", between),
        ("workflow_map", "Workflow map", links),
        ("dollar_figures", "Dollar figures", money),
    ]
    return [{"key": k, "label": label, "status": s, "reason": reason} for k, label, (s, reason) in rows]


def readiness(events: list, prices: bool = False) -> dict:
    """The dataset-level check: attributes, the grade the server will give,
    and per workflow its grade and the findings possible. Workflows are
    numbered, not named: the caller adds names only when asked to."""
    if not events:
        raise ValueError("no events")
    flows = workflows(events)
    by_workflow = defaultdict(list)
    for e in events:
        by_workflow[e.workflow].append(e)
    quality = check_data_quality(events)
    per_workflow = [findings_possible(by_workflow[w["name"]], w["runs"], prices) for w in flows]
    return {
        "dataset": {
            "events": len(events),
            "workflows": len(flows),
            "attributes": quality["attributes"],
            # the server grades the whole upload against its largest workflow's runs
            "grade": grade_dataset(events, flows[0]["runs"]),
            "small": len(events) < SMALL_DATASET_EVENTS,
            "small_threshold": SMALL_DATASET_EVENTS,
        },
        "findings_possible": summarize_findings(per_workflow),
        "workflows": [
            {
                "index": i,
                "events": w["events"],
                "runs": w["runs"],
                # what the server's report on this workflow shows as its grade
                "grade": grade_dataset(events, w["runs"]),
                "findings_possible": found,
            }
            for i, (w, found) in enumerate(zip(flows, per_workflow), start=1)
        ],
    }


def summarize_findings(per_workflow: list[list[dict]]) -> list[dict]:
    """One line per kind of finding for the whole dataset: ``yes`` when every
    workflow allows it, ``no`` when none does, else ``limited``."""
    rows = []
    for column in zip(*per_workflow):
        statuses = [f["status"] for f in column]
        allowed = sum(1 for s in statuses if s == "yes")
        status = "yes" if allowed == len(statuses) else "no" if all(s == "no" for s in statuses) else "limited"
        reasons = Counter(f["reason"] for f in column if f["status"] != "yes")
        reason = column[0]["reason"] if len(column) == 1 else (
            f"yes in all {len(column)} workflows" if status == "yes"
            else f"yes in {allowed} of {len(column)} workflows; {reasons.most_common(1)[0][0]}")
        rows.append({"key": column[0]["key"], "label": column[0]["label"], "status": status, "reason": reason})
    return rows


def remediation(report: dict, prices: bool) -> list[str]:
    """One sentence per gap, naming what to turn on or collect."""
    out = []
    by_key = {f["key"]: f for f in report["findings_possible"]}
    if by_key["reuse_result"]["status"] != "yes":
        out.append("Record tool arguments and results in your traces: without them repeated calls can't be matched.")
    money = by_key["dollar_figures"]
    if money["status"] != "yes" and "--prices" in money["reason"] and not prices:
        out.append("Pass token prices to estimate cost: --prices IN,OUT ($ per 1M input and output tokens).")
    thin = [w for w in report["workflows"] if w["runs"] < GOOD_RUNS]
    if thin:
        fewest = min(w["runs"] for w in thin)
        out.append(f"Collect a longer period: {len(thin)} workflow{'' if len(thin) == 1 else 's'} "
                   f"{'has' if len(thin) == 1 else 'have'} fewer than {GOOD_RUNS} runs (fewest: {fewest}).")
    if report["dataset"]["small"]:
        out.append(f"The traces hold {report['dataset']['events']:,} events; under "
                   f"{SMALL_DATASET_EVENTS:,} the analysis finds fewer rules.")
    return out
