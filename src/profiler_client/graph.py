"""The workflow graph: which step started which, from the events' own links.

An edge is drawn only where an event's ``parent`` names another event's
``event_id`` (contract v2). Nothing is inferred from the order events ran
in -- that is ``workflow.py``'s precedence, a different question. What a
link means is the dataset's ``parent_relation`` (see this package's
contract): for the known formats, "invokes" -- a tool call's parent is the
LLM call that chose it, or its enclosing span.

Two more kinds of edge come from what each LLM call records, not from
order: its ``trigger`` names the step whose result it read (or
``user_prompt``, drawn from a "prompt" start node), and with "invokes"
links an LLM call that started no step ended its run (drawn to a "reply"
end node). Each edge says its ``kind``: "invokes" (or "linked" for other
relations), "reads_result", "starts" or "ends".

A node is a step of one agent. A step used by more than one agent is one
node per agent, with the id ``"<step> @ <agent>"`` and the step in
``step``; a step only one agent uses keeps the step as its id, so a
workflow with one agent has the graph it always had. A trigger naming a
step several agents use is read from the latest call of that step in the
run, the reading agent's own if it made one.

Between agents (``handoffs``): a parent link from one agent's event to
another's is a handoff. Where one step hands over to more than one agent,
each of its handoffs says in ``fork`` whether that agent mostly starts
together with another from the same call ("both": they run side by side)
or alone ("either": a branch), and its share of the runs that reach the
step. A handoff that mostly re-enters an agent that already ran earlier in
the run is a return link: ``loop`` gives the runs that take it, their
share, and the average passes per such run (``passes_by_run`` defines it).

The agent graph (``agent_graph``) is the same graph one level up: a node
per agent, and a decision node per LLM step that hands over to another
agent. Its links: "reads" (a decision read that agent's result), "handoff"
(a decision started that agent; one link per pair, never to a single tool)
and "direct" (one agent's step started another agent with no LLM step
deciding). A handoff or direct link has a ``mode``: "both" (it mostly starts
that agent together with another), "return" (it mostly re-enters an agent
that already ran; ``loop`` as above) or "either" (anything else, a single
route included), and its ``share`` of the runs that reach where it starts.
An agent's ``rank`` is its place from the start of the run, ignoring return
links and the router: the agent owning the decisions behind at least half
of the handoffs, if one does (``router``).

When no event carries an id (a contract v1 dataset), or ids exist but no
parent resolves to one, the graph is unavailable and says why.
"""
from __future__ import annotations

from collections import Counter, defaultdict

from .contract import TraceEvent
from .evidence import evidence_sample

PROMPT, REPLY = "@prompt", "@reply"  # the run's start and end; real step names never start with "@"


GRAPH_VERSION = 3  # 2: one node per agent and step; fork and loop on handoffs. 3: agent_graph


def _trigger_steps(trigger: str) -> list[str]:
    return [part.removesuffix(" (error)") for part in trigger.split(" + ")]


def node_ids(events: list[TraceEvent]) -> dict[int, str]:
    """Each event's node id (by ``id(event)``): its step, or ``"<step> @
    <agent>"`` for a step more than one agent uses."""
    agents: dict[str, set] = defaultdict(set)
    for e in events:
        agents[e.node].add(e.agent)
    return {id(e): f"{e.node} @ {e.agent}" if len(agents[e.node]) > 1 else e.node for e in events}


def reentries(events: list[TraceEvent]) -> dict[int, bool]:
    """For each event that another agent's event handed over to (its
    ``parent`` is another agent's), whether its agent had already run in
    that run before the handover: a pass round a loop. Events in run order."""
    by_id = {e.event_id: e for e in events if e.event_id}
    pos = {id(e): i for i, e in enumerate(events)}
    first: dict[tuple, int] = {}
    for i, e in enumerate(events):
        first.setdefault((e.run_id, e.agent), i)
    out = {}
    for e in events:
        parent = by_id.get(e.parent) if e.parent else None
        if parent is not None and None not in (parent.agent, e.agent) and parent.agent != e.agent:
            out[id(e)] = first[(e.run_id, e.agent)] < pos[id(parent)]
    return out


def passes_by_run(events: list[TraceEvent]) -> dict[tuple[str, str], int]:
    """Passes round a loop into each agent, per ``(run, agent)``: the
    handovers that re-entered it (counted once per handing-over call)."""
    by_id = {e.event_id: e for e in events if e.event_id}
    calls: dict[tuple[str, str], set] = defaultdict(set)
    again = reentries(events)
    for e in events:
        if again.get(id(e)):
            calls[(e.run_id, e.agent)].add(by_id[e.parent].event_id)
    return {k: len(v) for k, v in calls.items()}


def build_graph(events: list[TraceEvent], workflow: str, relation: str | None) -> dict:
    """``{"availability": {...}, "graph": {...}}``; ``graph`` only when available."""
    events = [e for e in events if e.workflow == workflow]
    by_id: dict[str, TraceEvent] = {}
    for e in events:
        if e.event_id:
            by_id.setdefault(e.event_id, e)
    if not by_id:
        return {"availability": {"available": False, "code": "NO_EVENT_IDS"}}

    node = node_ids(events)
    links: dict[tuple[str, str], list[TraceEvent]] = defaultdict(list)
    started: set[str] = set()  # event ids that started another event
    for e in events:
        parent = by_id.get(e.parent) if e.parent else None
        if parent is not None and parent is not e:
            links[(node[id(parent)], node[id(e)])].append(e)
            started.add(parent.event_id)
    if not links:
        return {"availability": {"available": False, "code": "NO_PARENT_LINKS"}}
    parent_links = set(links)
    kinds = dict.fromkeys(links, "invokes" if relation == "invokes" else "linked")

    def add(source: str, target: str, kind: str, event: TraceEvent) -> None:
        kinds.setdefault((source, target), kind)
        links[(source, target)].append(event)

    steps_seen = {e.node for e in events}
    latest: dict[tuple, str] = {}  # (run, step) and (run, agent, step) -> node id of its latest call
    for e in events:
        if e.model is not None:  # only LLM calls record what they read
            for step in _trigger_steps(e.trigger) if e.trigger else []:
                if step == "user_prompt":
                    add(PROMPT, node[id(e)], "starts", e)
                elif step in steps_seen:
                    source = latest.get((e.run_id, e.agent, step)) or latest.get((e.run_id, step)) or step
                    if source != node[id(e)]:
                        add(source, node[id(e)], "reads_result", e)
            if relation == "invokes" and e.event_id and e.event_id not in started:
                add(node[id(e)], REPLY, "ends", e)
        latest[(e.run_id, e.node)] = latest[(e.run_id, e.agent, e.node)] = node[id(e)]

    occurrences = Counter(node[id(e)] for e in events)
    cost: dict[str, float] = defaultdict(float)
    agents: dict[str, set] = defaultdict(set)
    step_of: dict[str, str] = {}
    for e in events:
        cost[node[id(e)]] += e.cost
        agents[node[id(e)]].add(e.agent)
        step_of[node[id(e)]] = e.node
    llm = {node[id(e)] for e in events if e.model is not None}
    nodes = [
        {"id": n, "kind": "llm_call" if n in llm else "step",
         "agent": next(iter(agents[n])) if len(agents[n]) == 1 else None,
         "occurrences": count, "cost": round(cost[n], 6),
         **({"step": step_of[n]} if step_of[n] != n else {})}
        for n, count in occurrences.most_common()
    ]
    ends = {PROMPT: "start", REPLY: "end"}
    for end, kind in ends.items():
        touching = [c for (s, t), c in links.items() if end in (s, t)]
        if touching:
            nodes.append({"id": end, "kind": kind, "agent": None,
                          "occurrences": sum(map(len, touching)), "cost": 0.0})
    handoffs = _handoffs(links, parent_links, by_id, events, node)
    edges = [
        {
            "from": source,
            "to": target,
            "kind": kinds[(source, target)],
            "transition_count": len(children),
            "distinct_run_count": len({c.run_id for c in children}),
            "evidence_sample": evidence_sample(children),
            **(_handoff(children, by_id, handoffs) if (source, target) in parent_links else {}),
        }
        for (source, target), children in sorted(links.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    ]
    return {
        "availability": {"available": True},
        "graph": {
            "version": GRAPH_VERSION,
            "relation": relation or "unspecified",
            "run_count": len({e.run_id for e in events}),
            "nodes": nodes,
            "edges": edges,
            "agent_graph": _agent_graph(events, node, by_id, llm, links, kinds, handoffs),
        },
    }


def _agent_graph(events: list[TraceEvent], node: dict[int, str], by_id: dict[str, TraceEvent], llm: set,
                 links: dict, kinds: dict, h: dict) -> dict:
    """The graph one level up: agents and the decisions between them (see the module docstring)."""
    total = sum(e.cost for e in events)
    share = (lambda cost: round(cost / total, 3) if total else 0.0)
    runs: dict[str, set] = defaultdict(set)
    steps: dict[str, set] = defaultdict(set)
    cost: dict[str, float] = defaultdict(float)
    agent_of: dict[str, str] = {}  # node id -> its agent
    for e in events:
        if e.agent is not None:
            agent_of[node[id(e)]] = e.agent
            runs[e.agent].add(e.run_id)
            steps[e.agent].add(node[id(e)])
            cost[e.agent] += e.cost
    # handoffs: (start, agent) -> the calls that handed over, the runs, and the calls that re-entered
    handed: dict[tuple[str, str], dict] = defaultdict(lambda: {"calls": set(), "runs": set(), "again": set()})
    for e in events:
        parent = by_id.get(e.parent) if e.parent else None
        if parent is None or None in (parent.agent, e.agent) or parent.agent == e.agent:
            continue
        start = node[id(parent)] if node[id(parent)] in llm else parent.agent
        pair = handed[(start, e.agent)]
        pair["calls"].add(parent.event_id)
        pair["runs"].add(e.run_id)
        if h["reentries"][id(e)]:
            pair["again"].add(parent.event_id)
    decisions = sorted({start for start, _ in handed if start in llm})
    owner = {d: next(e.agent for e in events if node[id(e)] == d) for d in decisions}
    calls = Counter(node[id(e)] for e in events)
    dcost: dict[str, float] = defaultdict(float)
    for e in events:
        dcost[node[id(e)]] += e.cost
    runs_of = {**{a: runs[a] for a in runs}, **{d: h["runs_of_step"][d] for d in decisions}}

    read: dict[tuple[str, str], set] = defaultdict(set)  # (agent, decision) -> runs in which it read the result
    for (source, target), children in links.items():
        if kinds[(source, target)] == "reads_result" and target in owner and source in agent_of:
            read[(agent_of[source], target)] |= {c.run_id for c in children}
    out_links = [{"kind": "reads", "from_agent": a, "decision": d, "runs": len(r)} for (a, d), r in sorted(read.items())]
    for (start, agent), x in sorted(handed.items()):
        link = {"kind": "handoff", "decision": start} if start in owner else {"kind": "direct", "from_agent": start}
        link["to_agent"] = agent
        targets = {t for s, t in handed if s == start}
        together = sum(len(h["targets_of_call"][c]) > 1 for c in x["calls"])
        if 2 * len(x["again"]) > len(x["calls"]):
            mode = "return"
            passes = sum(h["passes"].get((run, agent), 0) for run in x["runs"])
            link["loop"] = {"runs": len(x["runs"]), "share_of_runs": round(len(x["runs"]) / h["run_count"], 3),
                            "passes": round(passes / len(x["runs"]), 2)}
        else:
            mode = "both" if len(targets) > 1 and 2 * together > len(x["calls"]) else "either"
        out_links.append({**link, "mode": mode, "runs": len(x["runs"]),
                          "share": round(len(x["runs"]) / len(runs_of[start]), 3)})

    alongside: dict[str, set] = defaultdict(set)
    for d in decisions:
        both = {x["to_agent"] for x in out_links if x.get("decision") == d and x.get("mode") == "both"}
        for a in both:
            alongside[a] |= both - {a}
    weight = Counter(owner[x["decision"]] for x in out_links if x["kind"] == "handoff")
    moves = sum(x["kind"] != "reads" for x in out_links)
    router = next((a for a, n in weight.most_common(1) if 2 * n >= moves), None)
    rank = _ranks(sorted(runs), out_links, router)
    agents = [{"id": a, "runs": len(runs[a]), "steps": len(steps[a]), "step_ids": sorted(steps[a]),
               "cost": round(cost[a], 6), "share": share(cost[a]), "rank": rank.get(a),
               "alongside": sorted(alongside[a])}
              for a in sorted(runs, key=lambda a: (rank.get(a, -1), -len(runs[a]), a))]
    return {
        "router": router,
        "agents": agents,
        "decisions": [{"id": d, "agent": owner[d], "calls": calls[d], "cost": round(dcost[d], 6),
                       "share": share(dcost[d])} for d in decisions],
        "links": out_links,
    }


def _crosses(child: TraceEvent, by_id: dict[str, TraceEvent]) -> bool:
    """Whether ``child``'s parent is another agent's event: a handoff."""
    parent = by_id[child.parent]
    return None not in (parent.agent, child.agent) and parent.agent != child.agent


def _handoffs(links: dict, parent_links: set, by_id: dict[str, TraceEvent], events: list[TraceEvent],
              node: dict[int, str]) -> dict:
    """What ``_handoff`` needs from the whole graph, worked out once."""
    targets_of_call: dict[str, set] = defaultdict(set)  # handing-over event id -> agents it started
    targets_of_step: dict[str, set] = defaultdict(set)  # handing-over node -> agents it starts
    step_of_call: dict[str, str] = {}
    for (source, target), children in links.items():
        if (source, target) in parent_links:
            for c in children:
                if _crosses(c, by_id):
                    targets_of_call[c.parent].add(c.agent)
                    targets_of_step[source].add(c.agent)
                    step_of_call[c.parent] = source
    runs_of_step: dict[str, set] = defaultdict(set)  # runs in which the handing-over node ran
    for e in events:
        if node[id(e)] in targets_of_step:
            runs_of_step[node[id(e)]].add(e.run_id)
    return {"targets_of_call": targets_of_call, "targets_of_step": targets_of_step, "step_of_call": step_of_call,
            "runs_of_step": runs_of_step, "reentries": reentries(events), "passes": passes_by_run(events),
            "run_count": len({e.run_id for e in events})}


def _handoff(children: list[TraceEvent], by_id: dict[str, TraceEvent], h: dict) -> dict:
    """``fork`` and ``loop`` for a parent link from one agent to another
    (see the module docstring); nothing for a link within one agent."""
    handed = [c for c in children if _crosses(c, by_id)]
    if not handed:
        return {}
    out = {}
    step = h["step_of_call"][handed[0].parent]
    if len(h["targets_of_step"][step]) > 1:
        calls = {c.parent for c in handed}
        together = sum(len(h["targets_of_call"][call]) > 1 for call in calls)
        runs = len({c.run_id for c in handed})
        out["fork"] = {"mode": "both" if 2 * together > len(calls) else "either",
                       "share_of_runs": round(runs / len(h["runs_of_step"][step]), 3)}
    again = [c for c in handed if h["reentries"][id(c)]]
    if 2 * len(again) > len(handed):
        runs = {c.run_id for c in again}
        passes = sum(h["passes"].get((run, again[0].agent), 0) for run in runs)
        out["loop"] = {"runs": len(runs), "share_of_runs": round(len(runs) / h["run_count"], 3),
                       "passes": round(passes / len(runs), 2)}
    return out


def _ranks(agents: list[str], links: list[dict], router: str | None) -> dict[str, int]:
    """Each agent's place from the start of the run: the longest chain of
    handoffs leading to it, ignoring return links and the router."""
    into: dict[str, set] = defaultdict(set)  # agent -> the agents whose work leads to it
    readers: dict[str, set] = defaultdict(set)  # decision -> the agents whose result it read
    for x in links:
        if x["kind"] == "reads":
            readers[x["decision"]].add(x["from_agent"])
    for x in links:
        if x["kind"] != "reads" and x["mode"] != "return":
            into[x["to_agent"]] |= readers[x["decision"]] if x["kind"] == "handoff" else {x["from_agent"]}
    rank = {a: 0 for a in agents if a != router}
    for _ in agents:  # at most one pass per agent; a cycle left in the links stops growing there
        changed = False
        for a in sorted(rank):
            best = max((rank[b] + 1 for b in into[a] if b in rank and b != a), default=0)
            if best > rank[a] and best < len(agents):
                rank[a], changed = best, True
        if not changed:
            break
    return rank
