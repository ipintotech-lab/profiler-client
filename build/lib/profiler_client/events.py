"""Convert your own agent's event log -- one event per record, in the
profiler's event shape -- into pseudonymized contract v2 events. Field
names may use the same aliases the profiler server accepts (``traceId``,
``spanId``, ``duration_ms``, ``prompt_tokens``, ``tool_name``, ...), and
the records may be wrapped (``{"events": [...]}``).

Every record needs a trace, run, workflow, event type and node. Then:

* **Ids** (trace, run, the event's own id, parent) are pseudonyms.
* **Names** -- workflow, agent, node, tool, event type, decision type and
  selection, trigger -- are pseudonyms unless ``--keep-names``.
  ``llm``/``llm_call``/``chat``/``completion`` events become ``decision``
  and ``tool``/``function_call`` events ``tool_call``. A decision with no
  ``decision.selected`` gets the one the log shows from what followed it
  and how it finished (``fill_selections``).
* **Contents** -- ``input_state`` (one pseudonym for the whole dict) and
  ``result`` (``success:``/``error:`` plus a pseudonym of the value) --
  are always pseudonyms.
* **Kept as is:** tokens, latency, cost, the cache and side-effect flags,
  ``input_complete``, a semver ``workflow_version`` and known model ids.
* **Dropped:** ``user_input``, ``invalidation_keys`` and any other field.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from . import policy
from .claude_code import Conversion, is_llm_node, llm_node
from .names import Names, version

_ALIASES: dict[str, tuple[str, ...]] = {
    "trace_id": ("trace_id", "traceId", "traceID", "trace"),
    "run_id": ("run_id", "runId", "spanId", "span_id", "call_id"),
    "workflow": ("workflow", "workflow_name", "workflowName"),
    "agent": ("agent", "agent_name", "agentName"),
    "event_type": ("event_type", "eventType", "type", "kind"),
    "node": ("node", "name", "span_name", "spanName"),
    "event_id": ("event_id", "eventId", "id", "spanId", "span_id"),
    "parent": ("parent", "parentSpanId", "parent_span_id", "parent_run_id"),
    "model": ("model", "model_name", "modelName"),
    "latency_ms": ("latency_ms", "duration_ms", "durationMs"),
    "cost": ("cost", "cost_usd", "costUsd"),
    "result": ("result", "output", "outputValue", "output_value"),
    "workflow_version": ("workflow_version", "workflowVersion"),
    "trigger": ("trigger", "triggered_by", "triggeredBy"),
    "has_side_effects": ("has_side_effects", "hasSideEffects", "side_effects", "sideEffects"),
    "idempotent": ("idempotent", "isIdempotent"),
    "cache_eligible": ("cache_eligible", "cacheEligible", "cacheable"),
    "cache_ttl_seconds": ("cache_ttl_seconds", "cacheTtlSeconds", "ttl_seconds"),
}
_REQUIRED = ("trace_id", "run_id", "workflow", "event_type", "node")
_CONTAINERS = ("events", "data", "traces", "runs", "items", "results", "rows")

_DECISION_TYPES = {"decision", "llm", "llm_call", "chat", "completion", "generation"}
_TOOL_TYPES = {"tool", "tool_call", "function_call"}
_ERROR_STATUSES = {"error", "failed", "failure"}
_FLAGS = ("has_side_effects", "idempotent", "cache_eligible", "input_complete")


def _get(raw: dict, field: str) -> Any:
    return next((raw[k] for k in _ALIASES.get(field, (field,)) if raw.get(k) is not None), None)


def located(rows: list) -> list[tuple[list, Any]]:
    """``[{"events": [...]}]`` (one wrapped document) -> the events, each
    with where it is in ``rows``: ``[0, "events", 3]``."""
    if len(rows) == 1 and isinstance(rows[0], dict):
        for key in _CONTAINERS:
            inner = rows[0].get(key)
            if isinstance(inner, list):
                return [([0, key, *where], row) for where, row in located(inner)]
            if isinstance(inner, dict):  # one event, not in a list: drop the list's index
                return [([0, key, *where[1:]], row) for where, row in located([inner])]
    return [([i], row) for i, row in enumerate(rows)]


def unwrap(rows: list) -> list:
    return [row for _, row in located(rows)]


def is_event_log(rows: list) -> bool:
    rows = unwrap(rows)
    return bool(rows) and all(
        isinstance(r, dict) and all(_get(r, f) not in (None, "") for f in _REQUIRED) for r in rows
    )


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _tokens(raw: dict) -> dict:
    tokens = raw.get("tokens") if isinstance(raw.get("tokens"), dict) else {}
    found = {
        "input": tokens.get("input", next((raw[k] for k in ("input_tokens", "inputTokens", "prompt_tokens",
                                                             "promptTokens") if k in raw), 0)),
        "output": tokens.get("output", next((raw[k] for k in ("output_tokens", "outputTokens", "completion_tokens",
                                                              "completionTokens") if k in raw), 0)),
    }
    return {k: int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else 0
            for k, v in found.items()}


def _convert(raw: dict, names: Names) -> dict:
    event_type = str(_get(raw, "event_type"))
    event: dict[str, Any] = {
        "trace_id": names.token("id", _get(raw, "trace_id")),
        "run_id": names.token("id", _get(raw, "run_id")),
        "workflow": names.step(str(_get(raw, "workflow"))),
        "event_type": "decision" if event_type in _DECISION_TYPES else "tool_call" if event_type in _TOOL_TYPES
        else names.step(event_type),
        "node": names.step(str(_get(raw, "node"))),
        "tokens": _tokens(raw),
    }
    if _get(raw, "agent") is not None:
        event["agent"] = names.step(str(_get(raw, "agent")))
    if _get(raw, "event_id") is not None:
        event["event_id"] = names.token("id", _get(raw, "event_id"))
    if _get(raw, "parent") is not None:
        event["parent"] = names.token("id", _get(raw, "parent"))
    if _get(raw, "model") is not None:
        event["model"] = names.model(_get(raw, "model"))
    tool = raw.get("tool")
    tool_name = tool.get("name") if isinstance(tool, dict) else raw.get("tool_name") or raw.get("toolName")
    if tool_name is not None:
        event["tool"] = {"name": names.step(str(tool_name))}
    decision = raw.get("decision")
    if isinstance(decision, dict) and decision.get("type") is not None and decision.get("selected") is not None:
        kind = str(decision["type"])
        event["decision"] = {"type": kind if kind == "next_action" else names.step(kind),
                             "selected": names.steps(str(decision["selected"]), "+")}
    trigger = _get(raw, "trigger")
    if trigger is not None:
        event["trigger"] = "user_prompt" if trigger == "user_prompt" else names.steps(str(trigger), " + ")
    if isinstance(raw.get("input_state"), dict):
        event["input_state"] = names.input_fields(raw["input_state"])
    result, error = _get(raw, "result"), raw.get("error")
    status = "error" if error or str(raw.get("status", "")).lower() in _ERROR_STATUSES else "success"
    if result is not None or error:
        event["result"] = f"{status}:{names.token('output', result if result is not None else error)}"
    for field in ("latency_ms", "cost"):
        number = _number(_get(raw, field))
        if number is not None and number >= 0:
            event[field] = number
    if version(_get(raw, "workflow_version")):
        event["workflow_version"] = _get(raw, "workflow_version")
    for flag in _FLAGS:
        if isinstance(_get(raw, flag), bool):
            event[flag] = _get(raw, flag)
    ttl = _get(raw, "cache_ttl_seconds")
    if isinstance(ttl, int) and not isinstance(ttl, bool) and ttl >= 0:
        event["cache_ttl_seconds"] = ttl
    return event


def _after(events: list[dict]) -> list[str | None]:
    """For each decision, the step it came after: its ``trigger``, else the
    last tool its agent called in this visit (same run, agent and parent),
    else the step that started the visit, else ``user_prompt``. None for
    other events."""
    by_id = {e["event_id"]: e for e in events if e.get("event_id")}
    last_tool: dict[tuple, str] = {}
    out: list[str | None] = []
    for e in events:
        visit = (e["run_id"], e.get("agent"), e.get("parent"))
        if e["event_type"] == "tool_call":
            last_tool[visit] = e["node"]
        if e["event_type"] != "decision":
            out.append(None)
            continue
        parent = by_id.get(e.get("parent"))
        named = parent is not None and not is_llm_node(parent["node"])
        out.append(e.get("trigger") or last_tool.get(visit) or (parent["node"] if named else "user_prompt"))
    return out


def _returned(events: list[dict], after: list[str | None]) -> list[str | None]:
    """The steps each decision reacted to, as OpenTelemetry names them: when
    agents it handed to have finished since its agent's previous decision,
    their last tools and the last tool its own agent called since then
    (``a + b``); else ``after``. A trigger the log wrote is kept."""
    visit_of = [(e["run_id"], e.get("agent"), e.get("parent")) for e in events]
    by_id = {e["event_id"]: i for i, e in enumerate(events) if e.get("event_id")}
    last_tool: dict[tuple, str] = {}
    ended: dict[tuple, int] = {}  # a visit -> where its last event is
    for i, (e, visit) in enumerate(zip(events, visit_of)):
        ended[visit] = i
        if e["event_type"] == "tool_call":
            last_tool[visit] = e["node"]
    started: dict[tuple, set] = defaultdict(set)  # a visit -> the visits its decisions started
    for e, visit in zip(events, visit_of):
        i = by_id.get(e.get("parent"))
        if i is not None and events[i].get("agent") != e.get("agent"):
            started[visit_of[i]].add(visit)
    out = list(after)
    prev: dict[tuple, int] = {}  # a visit -> its previous decision
    own: dict[tuple, str] = {}  # a visit -> the last tool it called since then
    for i, (e, visit) in enumerate(zip(events, visit_of)):
        if e["event_type"] == "tool_call":
            own[visit] = e["node"]
        if e["event_type"] != "decision":
            continue
        start = prev.get(visit, -1)
        tools = {last_tool[v] for v in started[visit] if v in last_tool and start < ended[v] < i}
        if tools and not e.get("trigger"):
            out[i] = " + ".join(sorted(tools | ({own[visit]} if visit in own else set())))
        prev[visit] = i
        own.pop(visit, None)
    return out


_TOOL_FINISH = frozenset({"tool_calls", "tool_use", "function_call"})


def _finish(raw: dict) -> str | None:
    """Why the model call stopped, as the log wrote it (``finish_reason``,
    ``stop_reason``, or a bare reason as its ``result``)."""
    for key in ("finish_reason", "finishReason", "stop_reason", "stopReason", "result"):
        value = raw.get(key)
        if isinstance(value, str) and value in policy.STOP_REASONS | _TOOL_FINISH:
            return value
    return None


def _followers(events: list[dict]) -> tuple[list[dict | None], dict[str, set]]:
    """Each event's next event in its visit (same run, agent and parent),
    and for each event id the agents of the events parented to it."""
    nxt: list[dict | None] = [None] * len(events)
    pending: dict[tuple, int] = {}
    handed: dict[str, set] = defaultdict(set)
    for i, e in enumerate(events):
        visit = (e["run_id"], e.get("agent"), e.get("parent"))
        if visit in pending:
            nxt[pending.pop(visit)] = e
        if e["event_type"] == "decision":
            pending[visit] = i
        if e.get("parent"):
            handed[e["parent"]].add(e.get("agent"))
    return nxt, handed


def fill_selections(events: list[dict], finishes: list[str | None]) -> None:
    """A decision the log gives no ``decision.selected`` picked what followed
    it, as the other readers take a model call's choice from what it called:
    a tool-calling finish reason followed by a tool call picked that tool;
    events of other agents parented to it are a handoff to them; a call that
    stopped with neither answered (``respond``, as the OpenTelemetry and chat
    readers name a call that called no tool). Anything else is left without
    a selection: the log can't tell what it picked."""
    nxt, handed = _followers(events)
    for e, after, finish in zip(events, nxt, finishes):
        if e["event_type"] != "decision" or (e.get("decision") or {}).get("selected") is not None:
            continue
        chosen = handed.get(e.get("event_id"), set()) - {e.get("agent")}
        if finish in _TOOL_FINISH and after is not None and after["event_type"] == "tool_call":
            chosen.add(after["node"])
        if not chosen and finish in policy.STOP_REASONS - _TOOL_FINISH:
            chosen = {"respond"}
        if chosen:
            e["decision"] = {"type": "next_action", "selected": "+".join(sorted(chosen))}


def _picks(events: list[dict]) -> list[str | None]:
    """For each decision, what the log shows it picked: ``"step"`` when its
    ``decision.selected`` is what came next -- the tool call that followed
    in its visit, or a handoff to another agent; ``"respond"`` when it
    selected that and ended the visit; None when it did its own work (a
    template, a score) or the log can't tell."""
    nxt, handed = _followers(events)
    out: list[str | None] = []
    for e, after in zip(events, nxt):
        selected = (e.get("decision") or {}).get("selected")
        if e["event_type"] != "decision" or selected is None:
            out.append(None)
            continue
        handoff = handed.get(e.get("event_id"), set()) - {e.get("agent")}
        tool = after["node"] if after is not None and after["event_type"] == "tool_call" else None
        chosen = set(selected.split("+"))
        if chosen == {"respond"}:
            out.append("respond" if after is None and not handoff else None)
        else:
            out.append("step" if chosen <= handoff | ({tool} if tool else set()) else None)
    return out


def split_shared_llm_names(events: list[dict]) -> None:
    """A log that names every model turn alike (``next_action``, ``agent``)
    names no step: when one decision name is used by several agents, or
    comes after different steps and is a routing turn -- every call picks
    the next step (a tool call, a handoff, the final reply; see ``_picks``)
    and some pick a tool or an agent -- each of its calls is renamed as
    OpenTelemetry names a turn it has no name for: by the step it came
    after (``llm_call ← <step>``), so turns of different agents and places
    in the flow are told apart. A named step that does its own work keeps
    its name."""
    after = _after(events)
    picks = _picks(events)
    agents: dict[str, set] = defaultdict(set)
    steps: dict[str, set] = defaultdict(set)
    kinds: dict[str, set] = defaultdict(set)
    for e, a, pick in zip(events, after, picks):
        if a is not None:
            agents[e["node"]].add(e.get("agent"))
            steps[e["node"]].add(a)
            kinds[e["node"]].add(pick)
    routing = {n for n, k in kinds.items() if None not in k and "step" in k}
    shared = {n for n in agents if len(agents[n]) > 1 or (len(steps[n]) > 1 and n in routing)}
    for e, a in zip(events, _returned(events, after)):
        if a is not None and e["node"] in shared:
            e["node"] = llm_node(a)


def convert_event_log(rows: list, names: Names) -> Conversion:
    rows = located(rows)
    events = [{**_convert(r, names), "_source": where} for where, r in rows]
    fill_selections(events, [_finish(r) for _, r in rows])
    split_shared_llm_names(events)
    traces = {e["trace_id"] for e in events}
    with_cost = {e["trace_id"] for e in events if e.get("cost", 0) > 0}
    return Conversion(events=events, sessions=len(traces), sessions_without_cost=len(traces - with_cost))
