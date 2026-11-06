"""Convert your own agent's event log -- one event per record, in the
profiler's event shape -- into pseudonymized contract v1 events. Field
names may use the same aliases the profiler server accepts (``traceId``,
``spanId``, ``duration_ms``, ``prompt_tokens``, ``tool_name``, ...), and
the records may be wrapped (``{"events": [...]}``).

Every record needs a trace, run, workflow, event type and node. Then:

* **Ids** (trace, run, parent) are pseudonyms.
* **Names** -- workflow, agent, node, tool, event type, decision type and
  selection, trigger -- are pseudonyms unless ``--keep-names``.
  ``llm``/``llm_call``/``chat``/``completion`` events become ``decision``
  and ``tool``/``function_call`` events ``tool_call``.
* **Contents** -- ``input_state`` (one pseudonym for the whole dict) and
  ``result`` (``success:``/``error:`` plus a pseudonym of the value) --
  are always pseudonyms.
* **Kept as is:** tokens, latency, cost, the cache and side-effect flags,
  ``input_complete``, a semver ``workflow_version`` and known model ids.
* **Dropped:** ``user_input``, ``invalidation_keys`` and any other field.
"""
from __future__ import annotations

from typing import Any

from .claude_code import Conversion
from .names import Names, version

_ALIASES: dict[str, tuple[str, ...]] = {
    "trace_id": ("trace_id", "traceId", "traceID", "trace"),
    "run_id": ("run_id", "runId", "spanId", "span_id", "call_id"),
    "workflow": ("workflow", "workflow_name", "workflowName"),
    "agent": ("agent", "agent_name", "agentName"),
    "event_type": ("event_type", "eventType", "type", "kind"),
    "node": ("node", "name", "span_name", "spanName"),
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


def unwrap(rows: list) -> list:
    """``[{"events": [...]}]`` (one wrapped document) -> the events."""
    if len(rows) == 1 and isinstance(rows[0], dict):
        for key in _CONTAINERS:
            if isinstance(rows[0].get(key), list):
                return unwrap(rows[0][key])
            if isinstance(rows[0].get(key), dict):
                return unwrap([rows[0][key]])
    return rows


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


def convert_event_log(rows: list, names: Names) -> Conversion:
    events = [_convert(r, names) for r in unwrap(rows)]
    traces = {e["trace_id"] for e in events}
    with_cost = {e["trace_id"] for e in events if e.get("cost", 0) > 0}
    return Conversion(events=events, sessions=len(traces), sessions_without_cost=len(traces - with_cost))
