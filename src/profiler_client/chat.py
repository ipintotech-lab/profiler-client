"""Convert OpenAI-style chat trajectories -- records of ``{"messages":
[{"role", "content", "tool_calls", "tool_call_id"}, ...]}``, as logged by
function-calling agents and datasets such as ``cx-cmu/agent_trajectories``
-- into contract v1 events:

* **LLM call** -- one per ``assistant`` message; ``decision.selected`` is
  the tools it called, or ``respond``. ``usage`` and ``finish_reason`` are
  used when the message carries them.
* **Tool call** -- one per ``tool_calls`` entry (or legacy
  ``function_call``), paired with its ``tool``/``function`` message by
  ``tool_call_id``, else in call order. A tool that takes a shell
  ``command`` is named by the command (``h:…: git status``).

Every tool, workflow and agent name is the user's own, so it is a
pseudonym unless ``--keep-names``. Ids, arguments and outputs are always
pseudonyms; message text is dropped. Trajectories carry no cost or
timing, so those stay 0.
"""
from __future__ import annotations

import json
import secrets
from typing import Any

from . import policy
from .claude_code import Conversion, Namer, llm_node
from .names import Names


def is_chat_trajectory(record: Any) -> bool:
    messages = record.get("messages") if isinstance(record, dict) else None
    return (
        isinstance(messages, list) and bool(messages)
        and all(isinstance(m, dict) and "role" in m for m in messages)
    )


def _arguments(raw: Any) -> dict:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {"arguments": raw}
    return raw if isinstance(raw, dict) else {} if raw is None else {"arguments": raw}


def _calls(message: dict) -> list[tuple[str | None, str, dict]]:
    calls = []
    for c in message.get("tool_calls") or []:
        if isinstance(c, dict):
            fn = c.get("function") if isinstance(c.get("function"), dict) else c
            calls.append((c.get("id"), fn.get("name") or "unknown", _arguments(fn.get("arguments"))))
    fn = message.get("function_call")
    if isinstance(fn, dict):
        calls.append((None, fn.get("name") or "unknown", _arguments(fn.get("arguments"))))
    return calls


def _convert(record: dict, names: Names, namer: Namer) -> list[dict]:
    record_id = record.get("id")
    base = record_id if record_id is not None else secrets.token_hex(16)
    trace = names.token("id", base)
    workflow = record.get("workflow") or record.get("benchmark") or record.get("task") or "chat"
    agent = record.get("agent")
    common = {
        "trace_id": trace,
        "run_id": trace,
        "workflow": names.step(str(workflow)),
        "agent": names.step(str(agent)) if agent else None,
    }
    record_model = record.get("model") or record.get("source_model")
    events: list[dict] = []
    open_calls: list[dict] = []  # in call order, waiting for their output
    trigger_parts: set[str] = set()

    for index, message in enumerate(record["messages"]):
        role = message.get("role")
        if role == "user":
            trigger_parts = {"user_prompt"}
        elif role in ("tool", "function"):
            call_id = message.get("tool_call_id")
            matches = [i for i, c in enumerate(open_calls) if call_id and c["_call_id"] == call_id]
            if not open_calls:
                continue
            call = open_calls.pop(matches[0] if matches else 0)
            call["result"] = f"success:{names.token('output', message.get('content'))}"
            trigger_parts.discard("user_prompt")
            trigger_parts.add(call["node"])
        elif role == "assistant":
            trigger = " + ".join(sorted(trigger_parts)) or "user_prompt"
            trigger_parts = set()
            usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
            llm = {
                **common,
                "event_type": "decision",
                "node": llm_node(trigger),
                "model": names.model(message.get("model") or record_model),
                "input_state": {"trigger": trigger},
                "input_complete": False,
                "trigger": trigger,
                "tokens": {"input": int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
                           "output": int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)},
            }
            finish = message.get("finish_reason")
            if finish in policy.STOP_REASONS:
                llm["result"] = finish
            events.append(llm)
            nodes = []
            for call_id, name, args in _calls(message):
                command = args.get("command") or args.get("cmd")
                if isinstance(command, str) and command.strip():
                    node = f"{names.step(name)}: {namer.bash_operation(command)}"
                else:
                    node = namer.tool_operation(name, args, frozenset())
                nodes.append(node)
                call = {
                    **common,
                    "event_type": "tool_call",
                    "node": node,
                    "parent": names.token("id", [base, index]),
                    "trigger": trigger,
                    "tool": {"name": names.step(name)},
                    "input_state": {"input": names.token("input", args)},
                    "input_complete": True,
                    "result": "no_result",
                    "_call_id": call_id,
                }
                events.append(call)
                open_calls.append(call)
            llm["decision"] = {"type": "next_action", "selected": "+".join(sorted(set(nodes))) or "respond"}
    return [{k: v for k, v in e.items() if v is not None and k != "_call_id"} for e in events]


def convert_chat_trajectories(rows: list, names: Names) -> Conversion:
    namer = Namer(names)
    records = [r for r in rows if is_chat_trajectory(r)]
    events = [e for r in records for e in _convert(r, names, namer)]
    return Conversion(events=events, sessions=len(records), sessions_without_cost=len(records))
