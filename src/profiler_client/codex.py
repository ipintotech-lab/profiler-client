"""Convert OpenAI Codex CLI sessions (``~/.codex/sessions/YYYY/MM/DD/
rollout-*.jsonl``) into contract v1 events, with the same rules as
``claude_code.py``:

* **LLM call** -- the model items between two inputs (a user message or
  tool outputs): its reasoning, messages and tool calls. Tokens come from
  the ``token_count`` record Codex writes after each response.
* **Tool call** -- one per ``function_call``, ``custom_tool_call``,
  ``local_shell_call``, ``web_search_call`` or ``tool_search_call``,
  paired with its output by ``call_id``. ``exec_command`` and ``shell``
  are named by the command they ran (``exec_command: git status``),
  ``apply_patch`` by the type of the first file it changes.

Kept in clear text: Codex's built-in tool names, policy words,
``gpt-``/``o``-series model ids and the CLI version. The project
directory, ids, MCP tool names, every input and every output are
pseudonyms; messages, reasoning and timestamps are dropped. With
``--llm-inputs`` an LLM call also records a pseudonym of every item the
model read in the session so far (``Names.llm_input``).

Codex records no cost, so every session counts in ``sessions_without_cost``.
Tool output headers that change on every run (chunk id, wall time, token
count, session id) are removed before the output is pseudonymized, so the
same command with the same output still matches; the exit code decides
``success`` or ``error``.
"""
from __future__ import annotations

import json
import re
import shlex
from pathlib import PurePosixPath
from typing import Any

from . import policy
from .claude_code import Conversion, Namer, llm_node, ts_ms
from .names import Names, version

_CALLS = ("function_call", "custom_tool_call", "local_shell_call", "web_search_call", "tool_search_call")
_OUTPUTS = ("function_call_output", "custom_tool_call_output", "local_shell_call_output", "tool_search_output")
_MODEL_ITEMS = ("reasoning",)

# Arguments that only explain or pace a call, not what it does.
_COSMETIC_ARGS = ("justification", "yield_time_ms")

_VOLATILE = re.compile(r"^(Chunk ID|Wall time|Original token count|Process running with session ID)\b.*$\n?", re.MULTILINE)
_EXIT = re.compile(r"^(?:Process exited with code|Exit code:) (\d+)", re.MULTILINE)


def is_codex_session(rows: list) -> bool:
    return any(
        isinstance(r, dict) and r.get("type") == "session_meta" and isinstance(r.get("payload"), dict)
        and ("cli_version" in r["payload"] or "originator" in r["payload"])
        for r in rows
    )


def _arguments(raw: Any) -> dict:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {"arguments": raw}
    return raw if isinstance(raw, dict) else {"arguments": raw}


def _command(value: Any) -> str | None:
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        # ["bash", "-lc", "git status"] runs the last element
        return value[-1] if len(value) == 3 and value[1] in ("-c", "-lc") else shlex.join(value)
    return value if isinstance(value, str) and value.strip() else None


def _patch_path(patch: Any) -> str | None:
    if not isinstance(patch, str):
        return None
    match = re.search(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", patch, re.MULTILINE)
    return match.group(1).strip() if match else None


def _output(item: dict) -> tuple[str, Any]:
    """(status, the output with its volatile headers removed)."""
    output = item.get("output")
    texts = [output] if isinstance(output, str) else [
        c.get("text") for c in output or [] if isinstance(c, dict) and isinstance(c.get("text"), str)
    ] if isinstance(output, list) else []
    status = "success"
    for text in texts:
        match = _EXIT.search(text)
        if match and match.group(1) != "0":
            status = "error"
    if isinstance(output, str):
        output = _VOLATILE.sub("", output)
    elif isinstance(output, list):
        output = [{**c, "text": _VOLATILE.sub("", c["text"])} if isinstance(c, dict) and isinstance(c.get("text"), str)
                  else c for c in output]
    else:
        output = {k: v for k, v in item.items() if k not in ("id", "call_id", "type")}
    return status, output


class _Codex:
    def __init__(self, names: Names) -> None:
        self.names = names
        self.namer = Namer(names)

    def node(self, item: dict) -> tuple[str, str, Any]:
        """(node, tool name, the input that decides the result)."""
        kind = item.get("type")
        if kind == "web_search_call":
            return "web_search", "web_search", item.get("action")
        if kind == "tool_search_call":
            return "tool_search", "tool_search", {k: v for k, v in item.items() if k not in ("id", "call_id", "status")}
        if kind == "local_shell_call":
            action = item.get("action") or {}
            command = _command(action.get("command"))
            node = f"local_shell: {self.namer.bash_operation(command)}" if command else "local_shell"
            return node, "local_shell", action
        name = item.get("name") or "unknown"
        if kind == "custom_tool_call":
            raw = item.get("input")
            if name == "apply_patch":
                path = _patch_path(raw)
                node = self.namer.tool_operation(name, {"path": path} if path else {}, policy.CODEX_TOOLS)
            else:
                node = self.namer.tool(name, policy.CODEX_TOOLS)
            return node, self.namer.tool(name, policy.CODEX_TOOLS).removeprefix("mcp "), raw
        args = _arguments(item.get("arguments"))
        namespace = item.get("namespace")
        if namespace:
            name = f"{namespace}__{name.lstrip('_')}"
        command = _command(args.get("cmd") if name == "exec_command" else args.get("command") if name == "shell" else None)
        if command:
            node = f"{name}: {self.namer.bash_operation(command)}"
        else:
            node = self.namer.tool_operation(name, args, policy.CODEX_TOOLS)
        tool = self.namer.tool(name, policy.CODEX_TOOLS).removeprefix("mcp ")
        return node, tool, {k: v for k, v in args.items() if k not in _COSMETIC_ARGS}


def convert_codex_session(rows: list[dict], names: Names) -> Conversion:
    rows = [r for r in rows if isinstance(r, dict)]
    codex = _Codex(names)
    events: list[dict] = []
    tool_calls: dict[str, dict] = {}
    session = cwd = cli_version = model = turn = None
    llm: dict | None = None  # the open LLM call, or the last one once closed
    open_llm = False
    trigger_parts: set[str] = set()
    previous_ms: float | None = None
    count = 0
    read = names.conversation()  # the items before the current one
    previous_item: dict | None = None

    for r in rows:
        kind, payload = r.get("type"), r.get("payload")
        record_ms = ts_ms(r)
        last_ms, previous_ms = previous_ms, record_ms if record_ms is not None else previous_ms
        if not isinstance(payload, dict):
            continue
        if kind == "session_meta" and session is None:
            session = payload.get("id") or payload.get("session_id")
            cwd, cli_version = payload.get("cwd"), payload.get("cli_version")
            continue
        if kind == "turn_context":
            model, turn = payload.get("model") or model, payload.get("turn_id") or turn
            continue
        if kind == "event_msg":
            if payload.get("type") == "task_started":
                turn = payload.get("turn_id") or turn
            elif payload.get("type") == "token_count" and llm is not None:
                usage = (payload.get("info") or {}).get("last_token_usage") or {}
                llm["tokens"]["input"] += int(usage.get("input_tokens") or 0)
                llm["tokens"]["output"] += int(usage.get("output_tokens") or 0)
            continue
        if kind != "response_item" or session is None:
            continue
        if previous_item is not None:
            read.add(previous_item)
        previous_item = payload

        item_type = payload.get("type")
        if item_type == "message" and payload.get("role") == "user":
            trigger_parts, open_llm = {"user_prompt"}, False
            continue
        if item_type in _OUTPUTS:
            open_llm = False
            call = tool_calls.get(payload.get("call_id"))
            if call is None:
                continue
            status, output = _output(payload)
            call["result"] = f"{status}:{names.token('output', output)}"
            start_ms = call.pop("_start_ms", None)
            if record_ms is not None and start_ms is not None:
                call["latency_ms"] = max(record_ms - start_ms, 0.0)
            trigger_parts.discard("user_prompt")
            trigger_parts.add(call["node"] + (" (error)" if status == "error" else ""))
            continue
        assistant = item_type == "message" and payload.get("role") == "assistant"
        if not (assistant or item_type in _CALLS or item_type in _MODEL_ITEMS):
            continue

        common = {
            "trace_id": names.token("id", session),
            "run_id": names.token("id", turn or session),
            "workflow": f"codex/{names.step(PurePosixPath(cwd or '').name or 'unknown', domain='id')}",
            "agent": "codex",
            "workflow_version": version(cli_version),
        }
        if not open_llm:
            count += 1
            trigger = " + ".join(sorted(trigger_parts)) or "user_prompt"
            trigger_parts, open_llm = set(), True
            llm_input = names.llm_input(model, read)
            llm = {
                **common,
                "event_type": "decision",
                "node": llm_node(trigger),
                "model": names.model(model),
                "input_state": {"trigger": trigger, **llm_input},
                "input_complete": bool(llm_input),
                "trigger": trigger,
                "tokens": {"input": 0, "output": 0},
                "decision": {"type": "next_action", "selected": "respond"},
                "_id": f"{session}:{count}",
                "_tools": [],
                "_start_ms": last_ms,
            }
            events.append(llm)
        if record_ms is not None and llm["_start_ms"] is not None:
            llm["latency_ms"] = max(record_ms - llm["_start_ms"], 0.0)
        if item_type not in _CALLS:
            continue
        node, tool, tool_input = codex.node(payload)
        llm["_tools"].append(node)
        llm["decision"]["selected"] = "+".join(sorted(set(llm["_tools"])))
        call = {
            **common,
            "event_type": "tool_call",
            "node": node,
            "parent": names.token("id", llm["_id"]),
            "trigger": llm["trigger"],
            "tool": {"name": tool},
            "input_state": {"input": names.token("input", tool_input)},
            "input_complete": True,
            "result": "no_result",
            "_start_ms": record_ms,
        }
        tool_calls[payload.get("call_id") or f"{session}:{len(events)}"] = call
        events.append(call)

    for event in events:
        for key in ("_id", "_tools", "_start_ms"):
            event.pop(key, None)
    return Conversion(events=events, sessions=1 if session else 0, sessions_without_cost=1 if session else 0)
