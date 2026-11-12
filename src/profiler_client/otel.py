"""Convert OpenTelemetry OTLP JSON trace exports (``{"resourceSpans": ...}``,
one document or one export request per line, as the collector's file
exporter writes) into contract v2 events.

Spans are read with the OpenTelemetry GenAI conventions (``gen_ai.*``)
and OpenInference's (``openinference.span.kind``, ``llm.*``, ``tool.*``),
which LangChain, LlamaIndex, OpenLLMetry, Langfuse, Phoenix and most agent
frameworks emit:

* **LLM call** -- ``gen_ai.operation.name`` ``chat``/``text_completion``/
  ``generate_content``, or span kind ``LLM``: model, tokens, finish reason.
* **Tool call** -- ``execute_tool`` or span kind ``TOOL``: the tool name,
  its arguments and result when the span records them.

Other spans (agents, chains, HTTP) only give structure and are skipped.
Within a trace, an LLM call's trigger is the tools that ran since the
previous LLM call, and its selection the tools that run until the next.

Service, workflow, agent and tool names are pseudonyms unless
``--keep-names``; ids, arguments, results and error messages always are.
A tool span that doesn't record its arguments is ``input_complete=False``,
so the profiler won't treat its calls as repeats of one input.
"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from . import policy
from .claude_code import Conversion, Namer, llm_node
from .names import Names

_LLM_OPERATIONS = ("chat", "text_completion", "generate_content")
_ERROR = 2  # OTLP STATUS_CODE_ERROR


def is_otel_export(rows: list) -> bool:
    return bool(rows) and all(isinstance(r, dict) and isinstance(r.get("resourceSpans"), list) for r in rows)


def _value(value: dict) -> Any:
    if "arrayValue" in value:
        return [_value(v) for v in (value["arrayValue"] or {}).get("values") or []]
    for key in ("stringValue", "boolValue", "doubleValue"):
        if key in value:
            return value[key]
    if "intValue" in value:
        try:
            return int(value["intValue"])  # a string in OTLP JSON
        except (TypeError, ValueError):
            return None
    return None


def _attributes(attributes: Any) -> dict:
    return {
        a["key"]: _value(a.get("value") or {})
        for a in attributes or [] if isinstance(a, dict) and isinstance(a.get("key"), str)
    }


def _first(attrs: dict, *keys: str) -> Any:
    return next((attrs[k] for k in keys if attrs.get(k) is not None), None)


def _int(value: Any) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def _parsed(value: Any) -> Any:
    """Arguments and results are often JSON text: parse them so equal
    values compare equal whatever their key order."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _spans(rows: list[dict]) -> list[tuple[dict, dict, dict, list]]:
    """Each span with its resource and attributes, and where it is."""
    spans = []
    for record, row in enumerate(rows):
        for r, resource_span in enumerate(row["resourceSpans"]):
            if not isinstance(resource_span, dict):
                continue
            resource = _attributes((resource_span.get("resource") or {}).get("attributes"))
            for s, scope in enumerate(resource_span.get("scopeSpans") or []):
                for n, span in enumerate((scope or {}).get("spans") or []):
                    if isinstance(span, dict) and span.get("traceId"):
                        where = [record, "resourceSpans", r, "scopeSpans", s, "spans", n]
                        spans.append((resource, _attributes(span.get("attributes")), span, where))
    return spans


def _kind(attrs: dict) -> str | None:
    operation = attrs.get("gen_ai.operation.name")
    span_kind = str(attrs.get("openinference.span.kind") or attrs.get("traceloop.span.kind") or "").upper()
    if operation == "execute_tool" or span_kind == "TOOL":
        return "tool"
    if operation in _LLM_OPERATIONS or span_kind == "LLM":
        return "llm"
    return None


def convert_otel_export(rows: list[dict], names: Names) -> Conversion:
    namer = Namer(names)
    by_trace: dict[str, list] = defaultdict(list)
    for resource, attrs, span, where in _spans(rows):
        kind = _kind(attrs)
        if kind:
            by_trace[span["traceId"]].append((int(span.get("startTimeUnixNano") or 0), kind, resource, attrs, span,
                                              where))

    events: list[dict] = []
    traces_with_cost: set[str] = set()
    for trace_id, spans in by_trace.items():
        spans.sort(key=lambda s: s[0])
        llm: dict | None = None
        trigger_parts: set[str] = set()
        for start, kind, resource, attrs, span, where in spans:
            end = int(span.get("endTimeUnixNano") or 0)
            conversation = _first(attrs, "gen_ai.conversation.id", "session.id")
            workflow = _first(attrs, "traceloop.workflow.name") or _first(resource, "service.name") or "otel"
            agent = _first(attrs, "gen_ai.agent.name")
            error = (span.get("status") or {}).get("code") in (_ERROR, "STATUS_CODE_ERROR")
            event = {
                "trace_id": names.token("id", conversation or trace_id),
                "run_id": names.token("id", trace_id),
                "workflow": names.step(str(workflow)),
                "agent": names.step(str(agent)) if agent else None,
                "event_id": names.token("id", span["spanId"]) if span.get("spanId") else None,
                "parent": names.token("id", span["parentSpanId"]) if span.get("parentSpanId") else None,
                "latency_ms": (end - start) / 1e6 if end > start else 0.0,
                "_source": where,
            }
            if kind == "llm":
                trigger = " + ".join(sorted(trigger_parts)) or "user_prompt"
                trigger_parts = set()
                finish = _first(attrs, "gen_ai.response.finish_reasons")
                finish = finish[0] if isinstance(finish, list) and finish else finish
                cost = _first(attrs, "gen_ai.usage.cost", "llm.cost.total")
                llm = {
                    **event,
                    "event_type": "decision",
                    "node": llm_node(trigger),
                    "model": names.model(_first(attrs, "gen_ai.response.model", "gen_ai.request.model",
                                                "llm.model_name")),
                    "input_state": {"trigger": trigger},
                    "input_complete": False,
                    "trigger": trigger,
                    "tokens": {
                        "input": _int(_first(attrs, "gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens",
                                             "llm.token_count.prompt")),
                        "output": _int(_first(attrs, "gen_ai.usage.output_tokens",
                                              "gen_ai.usage.completion_tokens", "llm.token_count.completion")),
                    },
                    "decision": {"type": "next_action", "selected": "respond"},
                    "result": finish if finish in policy.STOP_REASONS else None,
                    "_tools": [],
                }
                if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost > 0:
                    llm["cost"] = float(cost)
                    traces_with_cost.add(event["trace_id"])
                events.append(llm)
                continue
            name = str(_first(attrs, "gen_ai.tool.name", "tool.name") or span.get("name") or "unknown")
            args = _parsed(_first(attrs, "gen_ai.tool.call.arguments", "tool.parameters", "input.value",
                                  "traceloop.entity.input"))
            output = _parsed(_first(attrs, "gen_ai.tool.call.result", "output.value", "traceloop.entity.output"))
            command = args.get("command") or args.get("cmd") if isinstance(args, dict) else None
            if isinstance(command, str) and command.strip():
                node = f"{names.step(name)}: {namer.bash_operation(command)}"
            else:
                node = namer.tool_operation(name, args if isinstance(args, dict) else {}, frozenset())
            status = "error" if error else "success"
            if output is not None:
                result = f"{status}:{names.token('output', output)}"
            elif error:
                result = f"error:{names.token('output', (span.get('status') or {}).get('message'))}"
            else:
                result = "no_result"
            events.append({
                **event,
                "event_type": "tool_call",
                "node": node,
                "trigger": llm["trigger"] if llm else "user_prompt",
                "tool": {"name": names.step(name)},
                "input_state": {"input": names.token("input", args)} if args is not None else {},
                "input_complete": args is not None,
                "result": result,
            })
            trigger_parts.add(node + (" (error)" if error else ""))
            if llm is not None:
                llm["_tools"].append(node)
                llm["decision"]["selected"] = "+".join(sorted(set(llm["_tools"])))

    cleaned = [{k: v for k, v in e.items() if v is not None and k != "_tools"} for e in events]
    traces = {e["trace_id"] for e in cleaned}
    return Conversion(events=cleaned, sessions=len(traces), sessions_without_cost=len(traces - traces_with_cost))
