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

When no event carries an id (a contract v1 dataset), or ids exist but no
parent resolves to one, the graph is unavailable and says why.
"""
from __future__ import annotations

from collections import Counter, defaultdict

from .contract import TraceEvent
from .evidence import evidence_sample

PROMPT, REPLY = "@prompt", "@reply"  # the run's start and end; real step names never start with "@"


def _trigger_steps(trigger: str) -> list[str]:
    return [part.removesuffix(" (error)") for part in trigger.split(" + ")]


def build_graph(events: list[TraceEvent], workflow: str, relation: str | None) -> dict:
    """``{"availability": {...}, "graph": {...}}``; ``graph`` only when available."""
    events = [e for e in events if e.workflow == workflow]
    by_id: dict[str, TraceEvent] = {}
    for e in events:
        if e.event_id:
            by_id.setdefault(e.event_id, e)
    if not by_id:
        return {"availability": {"available": False, "code": "NO_EVENT_IDS"}}

    links: dict[tuple[str, str], list[TraceEvent]] = defaultdict(list)
    started: set[str] = set()  # event ids that started another event
    for e in events:
        parent = by_id.get(e.parent) if e.parent else None
        if parent is not None and parent is not e:
            links[(parent.node, e.node)].append(e)
            started.add(parent.event_id)
    if not links:
        return {"availability": {"available": False, "code": "NO_PARENT_LINKS"}}
    kinds = dict.fromkeys(links, "invokes" if relation == "invokes" else "linked")

    def add(source: str, target: str, kind: str, event: TraceEvent) -> None:
        kinds.setdefault((source, target), kind)
        links[(source, target)].append(event)

    nodes_seen = {e.node for e in events}
    for e in events:
        if e.model is None:
            continue  # only LLM calls record what they read
        for step in _trigger_steps(e.trigger) if e.trigger else []:
            if step == "user_prompt":
                add(PROMPT, e.node, "starts", e)
            elif step in nodes_seen and step != e.node:
                add(step, e.node, "reads_result", e)
        if relation == "invokes" and e.event_id and e.event_id not in started:
            add(e.node, REPLY, "ends", e)

    occurrences = Counter(e.node for e in events)
    cost: dict[str, float] = defaultdict(float)
    agents: dict[str, set] = defaultdict(set)
    for e in events:
        cost[e.node] += e.cost
        agents[e.node].add(e.agent)
    llm = {e.node for e in events if e.model is not None}
    nodes = [
        {"id": node, "kind": "llm_call" if node in llm else "step",
         "agent": next(iter(agents[node])) if len(agents[node]) == 1 else None,
         "occurrences": count, "cost": round(cost[node], 6)}
        for node, count in occurrences.most_common()
    ]
    ends = {PROMPT: "start", REPLY: "end"}
    for end, kind in ends.items():
        touching = [c for (s, t), c in links.items() if end in (s, t)]
        if touching:
            nodes.append({"id": end, "kind": kind, "agent": None,
                          "occurrences": sum(map(len, touching)), "cost": 0.0})
    edges = [
        {
            "from": source,
            "to": target,
            "kind": kinds[(source, target)],
            "transition_count": len(children),
            "distinct_run_count": len({c.run_id for c in children}),
            "evidence_sample": evidence_sample(children),
        }
        for (source, target), children in sorted(links.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    ]
    return {
        "availability": {"available": True},
        "graph": {
            "relation": relation or "unspecified",
            "run_count": len({e.run_id for e in events}),
            "nodes": nodes,
            "edges": edges,
        },
    }
