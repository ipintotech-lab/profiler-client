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

* **Named decision** -- an LLM call that records ``traceloop.entity.name``
  (a hand-instrumented step such as a handoff) is its own node, with its
  input (``traceloop.entity.input``, one pseudonym per field) and its
  choice (``traceloop.entity.output``), like a decision in an event log.

Other spans (agents, chains, HTTP) only give structure and are skipped.
Within an agent -- the spans under one agent span (``invoke_agent``, or
span kind ``AGENT``), or the whole trace when there is none -- an LLM
call's trigger is the tools that ran since the agent's previous LLM call,
and its selection the tools that run until the next. Agents running side
by side so don't mix their calls. A tool span's ``parent`` is its own
parent span when that is an LLM or tool call; when it is a skipped span
(an agent or chain), the parent is the LLM call that selected the tool, so
the map can still draw which call chose it.

Handoffs between agents: the first call in an agent span gets as parent
the call that handed over to it -- the span the agent span links to
(OTLP ``links``), else the enclosing agent's latest LLM call -- and an
agent's first LLM call is triggered by it. A call that names no agent takes
its agent span's ``gen_ai.agent.name``. When agents return, the next
LLM call of the agent they returned to is also triggered by each one's
last tool (not by an agent that handed over to another).

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


def _name_decision(llm: dict, names: Names, step: str, given: Any, chose: Any) -> None:
    """An LLM call the code named: the node, input and choice it recorded,
    in place of the tools around it."""
    node = names.step(step)
    llm["node"] = node
    if isinstance(given, dict):
        llm["input_state"] = names.input_fields(given)
    elif given is not None:
        llm["input_state"] = {"input": names.token("input", given)}
    llm["input_complete"] = given is not None
    if chose is not None:
        selected = names.steps(chose, "+") if isinstance(chose, str) else names.token("output", chose)
        llm["decision"] = {"type": node, "selected": selected}
        llm["result"] = f"success:{names.token('output', chose)}"


def _is_agent(attrs: dict) -> bool:
    span_kind = str(attrs.get("openinference.span.kind") or attrs.get("traceloop.span.kind") or "").upper()
    return attrs.get("gen_ai.operation.name") == "invoke_agent" or span_kind == "AGENT"


class _Agent:
    """What one agent span (``None``: the trace outside any) has done so far."""

    def __init__(self, span: dict | None, name: Any, outer: str | None) -> None:
        self.span = span or {}
        self.name = name  # its ``gen_ai.agent.name``, for calls that don't record one
        self.outer = outer  # the enclosing agent span's id
        self.llm: dict | None = None
        self.trigger_parts: set[str] = set()
        self.last_tool: str | None = None
        self.started = False  # has its first call been seen
        self.handed_over = False  # it handed the work to another agent instead of returning
        self.returned: list[str] = []  # agents inside it, in the order they started


class _Trace:
    """One trace's agent spans and the events converted so far."""

    def __init__(self, kept: dict[str, dict], others: dict[str, tuple[dict, dict]]) -> None:
        self.kept = kept  # converted spans by id
        self.others = others  # skipped spans by id, with their attributes
        self.agents: dict[str | None, _Agent] = {}
        self.events_by_span: dict[str, dict] = {}

    def agent_of(self, span_id: str | None) -> str | None:
        """The id of the agent span ``span_id`` sits in (itself, if it is one)."""
        seen: set[str] = set()
        while span_id and span_id not in seen:
            seen.add(span_id)
            if span_id in self.others:
                span, attrs = self.others[span_id]
                if _is_agent(attrs):
                    return span_id
                span_id = span.get("parentSpanId")
            elif span_id in self.kept:
                span_id = self.kept[span_id].get("parentSpanId")
            else:
                return None
        return None

    def state(self, agent_id: str | None) -> _Agent:
        if agent_id not in self.agents:
            span, attrs = self.others[agent_id] if agent_id else (None, {})
            outer = self.agent_of(span.get("parentSpanId")) if span else None
            self.agents[agent_id] = _Agent(span, _first(attrs, "gen_ai.agent.name"), outer)
            if agent_id:
                self.state(outer).returned.append(agent_id)
        return self.agents[agent_id]

    def handoff(self, here: _Agent) -> dict | None:
        """The call that handed over to this agent: its link, else the
        enclosing agent's latest LLM call."""
        for link in here.span.get("links") or []:
            if isinstance(link, dict) and link.get("spanId") in self.events_by_span:
                from_agent = self.agent_of(link["spanId"])
                if from_agent != here.outer and from_agent in self.agents:
                    self.agents[from_agent].handed_over = True
                return self.events_by_span[link["spanId"]]
        return self.state(here.outer).llm if here.span else None


def convert_otel_export(rows: list[dict], names: Names) -> Conversion:
    namer = Namer(names)
    by_trace: dict[str, list] = defaultdict(list)
    skipped: dict[str, dict[str, tuple[dict, dict]]] = defaultdict(dict)  # trace -> span id -> (span, attrs)
    for resource, attrs, span, where in _spans(rows):
        kind = _kind(attrs)
        if kind:
            by_trace[span["traceId"]].append((int(span.get("startTimeUnixNano") or 0), kind, resource, attrs, span,
                                              where))
        elif span.get("spanId"):
            skipped[span["traceId"]][span["spanId"]] = (span, attrs)

    events: list[dict] = []
    traces_with_cost: set[str] = set()
    for trace_id, spans in by_trace.items():
        spans.sort(key=lambda s: s[0])
        kept = {s[4].get("spanId") for s in spans}
        trace = _Trace({s[4]["spanId"]: s[4] for s in spans if s[4].get("spanId")}, skipped.get(trace_id, {}))
        for start, kind, resource, attrs, span, where in spans:
            here = trace.state(trace.agent_of(span.get("parentSpanId")))
            llm, trigger_parts = here.llm, here.trigger_parts
            first, here.started = not here.started, True
            source = trace.handoff(here) if first else None
            end = int(span.get("endTimeUnixNano") or 0)
            conversation = _first(attrs, "gen_ai.conversation.id", "session.id")
            workflow = _first(attrs, "traceloop.workflow.name") or _first(resource, "service.name") or "otel"
            agent = _first(attrs, "gen_ai.agent.name") or here.name
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
            if source is not None:
                event["parent"] = source["event_id"]
            if kind == "llm":
                for inner_id in here.returned:
                    inner = trace.agents[inner_id]
                    ended = int(inner.span.get("endTimeUnixNano") or 0) <= start
                    if ended and inner.last_tool and not inner.handed_over:
                        trigger_parts.add(inner.last_tool)
                here.returned = [a for a in here.returned
                                 if int(trace.agents[a].span.get("endTimeUnixNano") or 0) > start]
                trigger = " + ".join(sorted(trigger_parts)) or (source["node"] if source else "user_prompt")
                here.trigger_parts = set()
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
                step = _first(attrs, "traceloop.entity.name")
                if step is not None:
                    _name_decision(llm, names, str(step), _parsed(attrs.get("traceloop.entity.input")),
                                   _parsed(attrs.get("traceloop.entity.output")))
                if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost > 0:
                    llm["cost"] = float(cost)
                    traces_with_cost.add(event["trace_id"])
                events.append(llm)
                here.llm = llm
                if span.get("spanId"):
                    trace.events_by_span[span["spanId"]] = llm
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
            if llm is not None and span.get("parentSpanId") not in kept and source is None:
                event["parent"] = llm["event_id"]
            tool_event = {
                **event,
                "event_type": "tool_call",
                "node": node,
                "trigger": llm["trigger"] if llm else "user_prompt",
                "tool": {"name": names.step(name)},
                "input_state": {"input": names.token("input", args)} if args is not None else {},
                "input_complete": args is not None,
                "result": result,
            }
            events.append(tool_event)
            if span.get("spanId"):
                trace.events_by_span[span["spanId"]] = tool_event
            trigger_parts.add(node + (" (error)" if error else ""))
            here.last_tool = node
            if llm is not None:
                llm["_tools"].append(node)
                if llm["decision"]["type"] == "next_action":
                    llm["decision"]["selected"] = "+".join(sorted(set(llm["_tools"])))

    cleaned = [{k: v for k, v in e.items() if v is not None and k != "_tools"} for e in events]
    traces = {e["trace_id"] for e in cleaned}
    return Conversion(events=cleaned, sessions=len(traces), sessions_without_cost=len(traces - traces_with_cost))
