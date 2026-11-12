"""Convert Claude Code session transcripts (``~/.claude/projects/<project>/
<sessionId>.jsonl``) into contract v2 events, pseudonymized as they are
built. Adapted from the profiler server's converter; what changes is what
leaves the machine:

* **Kept in clear text:** Claude Code's built-in tool names, well-known
  programs and sub-commands (``git status``), file types (``.py``),
  ``claude-*`` model ids, the Claude Code version, token counts,
  latencies, cost and stop reasons -- the vocabulary in ``policy.py``.
* **Pseudonymized:** the project directory, session/prompt/message ids,
  MCP and custom tool names, script names, programs and sub-commands not
  in the policy, every tool input, and every tool output.
* **Dropped:** prompts, replies, tool output text, the user's request,
  timestamps (only durations are kept) and working directories.

An LLM call records ``input_complete=False`` (the model also saw the whole
conversation); a tool call records its whole input, so ``True``. With
``--llm-inputs`` an LLM call also records a pseudonym of everything it read
in its lane so far (``Names.llm_input``), and so ``True``; the transcript
doesn't hold Claude Code's system prompt, which follows its version.

Cost: transcripts carry only a session-level ``cost-state`` record with
real USD per model, split across that model's calls by price-weighted
tokens. A session without one gets ``cost=0`` and is counted in
``sessions_without_cost`` so the report can say cost wasn't observed.
"""
from __future__ import annotations

import shlex
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath

from . import policy
from .names import Conversation, Names, version

# Relative per-token price weights (input = 1), used only to split the
# session's recorded cost across calls.
_WEIGHT_INPUT = 1.0
_WEIGHT_CACHE_READ = 0.1
_WEIGHT_CACHE_WRITE_5M = 1.25
_WEIGHT_CACHE_WRITE_1H = 2.0
_WEIGHT_OUTPUT = 5.0

_SYNTHETIC_MODEL = "<synthetic>"

# Tool arguments that are only a human-readable label and don't change
# what the tool does -- dropped so the same command counts as the same input.
_COSMETIC_TOOL_ARGS = {"Bash": ("description",)}

# Arguments that name which operation a tool performs, checked in order.
_OPERATION_ARGS = ("action", "operation", "method", "subcommand", "mode", "type")
# Arguments holding a file path: the operation is the file type.
_PATH_ARGS = ("file_path", "notebook_path", "path")


@dataclass
class Conversion:
    events: list[dict] = field(default_factory=list)
    sessions: int = 0
    sessions_without_cost: int = 0
    # where each event came from, for ``show-event``: ``[record, *path]``, the
    # record's index in the file's rows and the JSON path inside it (or None).
    # A converter puts it on an event as ``_source``; it is taken off here.
    sources: list = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self.sources = [event.pop("_source", None) for event in self.events]


def is_claude_code_transcript(rows: list) -> bool:
    return any(
        isinstance(r, dict)
        and r.get("type") in ("assistant", "user")
        and "sessionId" in r
        and isinstance(r.get("message"), dict)
        for r in rows
    )


class Namer:
    """Turns raw tool calls into node names made only of policy words,
    pseudonyms and (with ``--keep-names``) the user's tool names."""

    def __init__(self, names: Names) -> None:
        self.names = names

    def name(self, value: str) -> str:
        return self.names.token("name", value)

    def tool(self, name: str, builtin: frozenset[str] = policy.BUILTIN_TOOLS) -> str:
        """A tool's own name: built-ins in clear text, MCP and custom tools are theirs."""
        if name in builtin:
            return name
        return f"mcp {self.names.step(name)}" if name.startswith("mcp__") else self.names.step(name)

    def bash_operation(self, command: str) -> str:
        segments = [seg.strip() for seg in command.replace("\n", "&&").replace(";", "&&").split("&&")]
        for seg in segments:
            try:
                tokens = shlex.split(seg)
            except ValueError:
                tokens = seg.split()
            while tokens and ("=" in tokens[0] and not tokens[0].startswith("=")):
                tokens = tokens[1:]  # VAR=value prefixes
            if tokens and tokens[0] == "timeout":
                tokens = [t for t in tokens[1:] if not t[:1].isdigit() and not t.startswith("-")]
            if not tokens or tokens[0] in ("cd", "export", "set", "source", "."):
                continue
            program = PurePosixPath(tokens[0]).name
            rest = [t for t in tokens[1:] if not t.startswith("-")]
            runner = "python" if program.startswith("python") else program
            if runner == "python" and len(tokens) > 2 and tokens[1] == "-m":
                module = tokens[2]
                return f"python -m {module if module in policy.PYTHON_MODULES else self.name(module)}"
            if runner in policy.RUNNERS:
                # a runner alone names no operation: tell scripts apart by
                # file and inline code by the whole command
                script = next((t for t in rest if PurePosixPath(t).suffix in policy.FILE_SUFFIXES), None)
                if script:
                    return f"{runner} {self.name(PurePosixPath(script).name)}"
                return f"{runner} inline:{self.name(command)}"
            subcommands = policy.SUBCOMMAND_PROGRAMS.get(program)
            if subcommands is not None and rest:
                return f"{program} {rest[0] if rest[0] in subcommands else self.name(rest[0])}"
            if subcommands is not None or program in policy.PLAIN_PROGRAMS:
                return program
            return self.name(program)
        return "other"

    def tool_operation(self, name: str, tool_input: dict, builtin: frozenset[str] = policy.BUILTIN_TOOLS) -> str:
        command = tool_input.get("command")
        if name == "Bash" and isinstance(command, str):
            return f"Bash: {self.bash_operation(command)}"
        tool = self.tool(name, builtin)
        for arg in _OPERATION_ARGS:
            value = tool_input.get(arg)
            if isinstance(value, str) and 0 < len(value) <= 40 and "\n" not in value:
                return f"{tool}: {self.name(value)}"
        for arg in _PATH_ARGS:
            value = tool_input.get(arg)
            if isinstance(value, str) and value:
                kind = PurePosixPath(value).suffix or PurePosixPath(value).name
                return f"{tool}: {kind if kind in policy.FILE_SUFFIXES else self.name(kind)}"
        return tool


def llm_node(trigger: str) -> str:
    """An LLM call is named by the first thing it reacted to."""
    first = trigger.split(" + ")[0].removesuffix(" (error)")
    return f"llm_call ← {first.split(': ')[0]}"


def ts_ms(record: dict) -> float | None:
    ts = record.get("timestamp")
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000
    except ValueError:
        # an unreadable time only costs a latency; its error would quote the value
        return None


def _weighted_tokens(usage: dict) -> float:
    cache_creation = usage.get("cache_creation") or {}
    write_1h = cache_creation.get("ephemeral_1h_input_tokens")
    write_5m = cache_creation.get("ephemeral_5m_input_tokens")
    if write_1h is None and write_5m is None:
        write_5m, write_1h = usage.get("cache_creation_input_tokens", 0), 0
    return (
        _WEIGHT_INPUT * usage.get("input_tokens", 0)
        + _WEIGHT_CACHE_READ * usage.get("cache_read_input_tokens", 0)
        + _WEIGHT_CACHE_WRITE_5M * (write_5m or 0)
        + _WEIGHT_CACHE_WRITE_1H * (write_1h or 0)
        + _WEIGHT_OUTPUT * usage.get("output_tokens", 0)
    )


def _user_trigger(record: dict, tool_nodes_by_id: dict[str, str]) -> str:
    """What an LLM call following this user record is reacting to, built
    from already-pseudonymized node names."""
    content = record["message"].get("content")
    if isinstance(content, str):
        return "user_prompt"
    tool_results = [c for c in content or [] if isinstance(c, dict) and c.get("type") == "tool_result"]
    if tool_results:
        names = sorted(
            {
                tool_nodes_by_id.get(c.get("tool_use_id"), "unknown") + (" (error)" if c.get("is_error") else "")
                for c in tool_results
            }
        )
        return " + ".join(names)
    return "user_prompt"


def convert_claude_code_transcript(rows: list[dict], names: Names) -> Conversion:
    records = list(enumerate(rows))
    rows = [r for r in rows if isinstance(r, dict)]
    namer = Namer(names)
    ps = names
    session_cost_by_model: dict[str, dict[str, float]] = defaultdict(dict)
    for r in rows:
        if r.get("type") == "cost-state":
            for model, usage in (r.get("modelUsage") or {}).items():
                session_cost_by_model[r.get("sessionId")][model] = float(usage.get("costUSD") or 0.0)

    llm_calls: dict[str, dict] = {}
    # Chronological order of all events (pattern mining depends on it).
    order: list[tuple[str, str]] = []
    tool_calls: dict[str, dict] = {}
    tool_nodes_by_id: dict[str, str] = {}
    sessions: set[str] = set()

    current_prompt: dict[str, str] = {}
    session_root: dict[str, str] = {}
    last_trigger: dict[str, str] = {}
    # Latest record of any type per lane: an LLM call is timed from the
    # record just before its reply, not from its trigger.
    last_seen_ms: dict[str, float] = {}
    # what the model has read so far, per lane
    read: dict[str, Conversation] = defaultdict(names.conversation)

    for record, r in records:
        if not isinstance(r, dict):
            continue
        kind = r.get("type")
        session = r.get("sessionId")
        record_ms = ts_ms(r)
        previous_ms = None
        if session and record_ms is not None:
            seen_lane = f"{session}:{bool(r.get('isSidechain'))}"
            previous_ms = last_seen_ms.get(seen_lane)
            last_seen_ms[seen_lane] = record_ms
        if kind not in ("user", "assistant") or not session or not isinstance(r.get("message"), dict):
            continue
        sessions.add(session)
        sidechain = bool(r.get("isSidechain"))
        lane = f"{session}:{sidechain}"

        if kind == "user":
            read[lane].add({"role": "user", "content": r["message"].get("content")})
            if r.get("promptId"):
                current_prompt[session] = r["promptId"]
            if r.get("isMeta"):
                continue
            last_trigger[lane] = _user_trigger(r, tool_nodes_by_id)
            for c in r["message"].get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("tool_use_id") in tool_calls:
                    call = tool_calls[c["tool_use_id"]]
                    status = "error" if c.get("is_error") else "success"
                    call["result"] = f"{status}:{ps.token('output', c.get('content'))}"
                    end_ms, start_ms = record_ms, call.pop("_start_ms", None)
                    if end_ms is not None and start_ms is not None:
                        call["latency_ms"] = max(end_ms - start_ms, 0.0)
            continue

        message = r["message"]
        raw_model = message.get("model")
        message_id = message.get("id") or r.get("uuid")
        if not message_id or raw_model == _SYNTHETIC_MODEL:
            read[lane].add({"role": "assistant", "content": message.get("content")})
            continue

        # Pin the workflow to the session's first cwd so a mid-session
        # ``cd`` doesn't split one session into several workflows.
        cwd = session_root.setdefault(session, r.get("cwd") or "")
        common = {
            "trace_id": ps.token("id", session),
            "run_id": ps.token("id", current_prompt.get(session, session)),
            "workflow": f"claude-code/{names.step(PurePosixPath(cwd).name or 'unknown', domain='id')}",
            "agent": "claude-code-subagent" if sidechain else "claude-code",
            "workflow_version": version(r.get("version")),
        }

        call = llm_calls.get(message_id)
        if call is None:
            trigger = last_trigger.get(lane, "user_prompt")
            llm_input = names.llm_input(raw_model, read[lane])
            call = {
                **common,
                "event_type": "decision",
                "event_id": ps.token("id", message_id),
                "node": llm_node(trigger),
                "model": names.model(raw_model),
                "input_state": {"trigger": trigger, **llm_input},
                "input_complete": bool(llm_input),
                "trigger": trigger,
                "_session": session,
                "_raw_model": raw_model,
                "_tools": [],
                "_start_ms": previous_ms,
                "_source": [record],
            }
            llm_calls[message_id] = call
            order.append(("llm", message_id))
        usage = message.get("usage") or {}
        call["_usage"] = usage
        stop = message.get("stop_reason")
        if stop in policy.STOP_REASONS:
            call["result"] = stop
        if record_ms is not None and call["_start_ms"] is not None:
            call["latency_ms"] = max(record_ms - call["_start_ms"], 0.0)

        for position, block in enumerate(message.get("content") or []):
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tool_id, name = block.get("id"), block.get("name") or "unknown"
            tool_input = block.get("input") or {}
            node = namer.tool_operation(name, tool_input)
            tool_nodes_by_id[tool_id] = node
            call["_tools"].append(node)
            kept = {k: v for k, v in tool_input.items() if k not in _COSMETIC_TOOL_ARGS.get(name, ())}
            tool_calls[tool_id] = {
                **common,
                "event_type": "tool_call",
                "event_id": ps.token("id", ["tool", tool_id if tool_id else [message_id, len(call["_tools"])]]),
                "node": node,
                "parent": ps.token("id", message_id),
                # a tool call starts from whatever the LLM call that chose it reacted to
                "trigger": call["trigger"],
                "tool": {"name": name if name in policy.BUILTIN_TOOLS else names.step(name)},
                # one pseudonym for the whole input: equal inputs stay equal,
                # and argument names of custom tools don't leak either
                "input_state": {"input": ps.token("input", kept)},
                "input_complete": True,
                "result": "no_result",
                "_start_ms": record_ms,
                "_source": [record, "message", "content", position],
            }
            order.append(("tool", tool_id))
        read[lane].add({"role": "assistant", "content": message.get("content")})

    weights: dict[tuple[str, str], float] = defaultdict(float)
    for call in llm_calls.values():
        call["_weight"] = _weighted_tokens(call["_usage"])
        weights[(call["_session"], call["_raw_model"])] += call["_weight"]

    for call in llm_calls.values():
        usage = call.pop("_usage")
        weight = call.pop("_weight")
        tools = call.pop("_tools")
        session, raw_model = call.pop("_session"), call.pop("_raw_model")
        call.pop("_start_ms")
        call["tokens"] = {
            "input": usage.get("input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0),
            "output": usage.get("output_tokens", 0),
        }
        call["decision"] = {"type": "next_action", "selected": "+".join(sorted(set(tools))) or "respond"}
        total = weights[(session, raw_model)]
        model_cost = session_cost_by_model.get(session, {}).get(raw_model, 0.0)
        call["cost"] = model_cost * weight / total if total else 0.0
    for call in tool_calls.values():
        call.pop("_start_ms", None)

    return Conversion(
        events=[llm_calls[key] if kind == "llm" else tool_calls[key] for kind, key in order],
        sessions=len(sessions),
        sessions_without_cost=len(sessions - set(session_cost_by_model)),
    )
