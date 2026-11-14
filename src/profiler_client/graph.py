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

When no event carries an id (a contract v1 dataset), or ids exist but no
parent resolves to one, the graph is unavailable and says why.
"""
from __future__ import annotations

from collections import Counter, defaultdict

from .contract import TraceEvent
from .evidence import evidence_sample

PROMPT, REPLY = "@prompt", "@reply"  # the run's start and end; real step names never start with "@"


GRAPH_VERSION = 2  # 2: one node per agent and step; fork and loop on handoffs


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
        },
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
