"""Claude Code subagents: one agent per subagent type, followed by
``agentId``, and a handoff from the parent's Agent tool call to the
subagent's first LLM call -- across the separate files Claude Code writes."""
import json

import pytest
from conftest import _assistant, _result, _tool_use, _user, canary_forms

from profiler_client.claude_code import convert_claude_code_transcript
from profiler_client.contract import TraceEvent
from profiler_client.dataset import EVENTS, prepare, read_sealed
from profiler_client.graph import PROMPT, build_graph
from profiler_client.lint import check_event
from profiler_client.names import Names
from profiler_client.pseudonym import Pseudonymizer, new_key

S = "sess-1"


def _side(row, agent_id, attribution=None, cwd=None):
    """A row as Claude Code writes it to ``subagents/agent-<agentId>.jsonl``."""
    row = {**row, "isSidechain": True, "agentId": agent_id}
    if attribution:
        row["attributionAgent"] = attribution
    if cwd:
        row["cwd"] = cwd
    return row


def _parent_rows(agent_type="Explore", agent_id="aaa", tool="Agent"):
    """The main conversation: it starts a subagent and reads its report."""
    done = _user(S, "2026-10-01T10:00:20Z", [_result("t-agent", "report")], prompt_id="p1")
    done["toolUseResult"] = {"status": "completed", "agentId": agent_id, "agentType": agent_type}
    return [
        _user(S, "2026-10-01T10:00:00Z", "look around", prompt_id="p1"),
        _assistant(S, "m1", "2026-10-01T10:00:01Z", [
            _tool_use("t-agent", tool, {"subagent_type": agent_type, "prompt": "list files"})], cwd="/home/a/proj"),
        done,
        _assistant(S, "m2", "2026-10-01T10:00:21Z", [{"type": "text", "text": "done"}], stop="end_turn",
                   cwd="/home/a/proj"),
    ]


def _subagent_rows(agent_id="aaa", attribution=None, cwd="/home/a/proj/sub"):
    """The subagent's own file: same session and prompt, its own agentId --
    and here another working directory, as when the subagent ``cd``s."""
    return [
        _side(_user(S, "2026-10-01T10:00:02Z", "list files", prompt_id="p1"), agent_id, attribution),
        _side(_assistant(S, f"{agent_id}-1", "2026-10-01T10:00:03Z", [_tool_use(f"{agent_id}-t", "Bash", {
            "command": "ls"})], cwd=cwd), agent_id, attribution),
        _side(_user(S, "2026-10-01T10:00:04Z", [_result(f"{agent_id}-t", "a.py")], prompt_id="p1"), agent_id,
              attribution),
        _side(_assistant(S, f"{agent_id}-2", "2026-10-01T10:00:05Z", [{"type": "text", "text": "a.py"}],
                         stop="end_turn", cwd=cwd), agent_id, attribution),
    ]


def _write(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def _prepare(tmp_path, files):
    out = tmp_path / "out"
    manifest = prepare(files, out, Names(Pseudonymizer(new_key())))
    _, events = read_sealed((out / EVENTS).read_bytes())
    return manifest, events


@pytest.mark.parametrize("subagent_first", [True, False])
def test_the_parents_agent_call_hands_over_to_the_subagent_across_files(tmp_path, subagent_first):
    parent = _write(tmp_path / "s.jsonl", _parent_rows())
    sub = _write(tmp_path / "agent-aaa.jsonl", _subagent_rows())  # no attributionAgent: type from the parent
    manifest, events = _prepare(tmp_path, [sub, parent] if subagent_first else [parent, sub])

    assert manifest["policy_version"] == "5"
    assert {e["agent"] for e in events} == {"claude-code", "claude-code-subagent:Explore"}
    # one workflow, though the subagent worked in another directory
    assert len({e["workflow"] for e in events}) == 1
    assert len({e["run_id"] for e in events}) == 1

    agent_call = next(e for e in events if e["node"] == "Agent")
    sub_calls = [e for e in events if e["agent"] != "claude-code" and e["event_type"] == "decision"]
    assert sub_calls[0]["parent"] == agent_call["event_id"]
    assert "parent" not in sub_calls[1]  # only the first call is handed over

    graph = build_graph([TraceEvent.model_validate(e) for e in events], events[0]["workflow"],
                        manifest["parent_relation"])["graph"]
    edges = {(e["from"], e["to"]): e for e in graph["edges"]}
    target = "llm_call ← user_prompt @ claude-code-subagent:Explore"
    assert edges[("Agent", target)]["kind"] == "invokes"
    # the subagent was started by the Agent call, not by the user's prompt
    assert (PROMPT, target) not in edges
    assert (PROMPT, "llm_call ← user_prompt @ claude-code") in edges
    # and the parent reads the subagent's report back from the Agent call
    assert edges[("Agent", "llm_call ← Agent")]["kind"] == "reads_result"


def test_one_file_with_both_conversations_links_too():
    events = convert_claude_code_transcript(_parent_rows() + _subagent_rows(), Names(Pseudonymizer(new_key()))).events
    agent_call = next(e for e in events if e["node"] == "Agent")
    first = next(e for e in events if e["agent"].startswith("claude-code-subagent:"))
    assert first["parent"] == agent_call["event_id"]


def test_the_older_task_tool_hands_over_the_same_way():
    rows = _parent_rows(tool="Task") + _subagent_rows()
    events = convert_claude_code_transcript(rows, Names(Pseudonymizer(new_key()))).events
    task = next(e for e in events if e["node"] == "Task")
    assert next(e for e in events if e["agent"] != "claude-code")["parent"] == task["event_id"]


def test_a_subagent_file_alone_has_no_link_and_names_itself():
    events = convert_claude_code_transcript(_subagent_rows(attribution="Plan"), Names(Pseudonymizer(new_key()))).events
    assert {e["agent"] for e in events} == {"claude-code-subagent:Plan"}
    assert all("parent" not in e for e in events if e["event_type"] == "decision")


def test_subagents_side_by_side_keep_their_own_lanes():
    a = _subagent_rows("aaa", attribution="Explore")
    b = _subagent_rows("bbb", attribution="Plan")
    # interleaved: A's Bash result lands between B's first and second call
    rows = [a[0], b[0], a[1], b[1], a[2], b[3], a[3]]
    events = convert_claude_code_transcript(rows, Names(Pseudonymizer(new_key()))).events
    plan = [e for e in events if e["agent"] == "claude-code-subagent:Plan" and e["event_type"] == "decision"]
    # B's second call reacts to its own prompt, not to A's Bash result
    assert [e["trigger"] for e in plan] == ["user_prompt", "user_prompt"]


def test_a_custom_subagent_type_is_a_pseudonym_and_a_built_in_is_kept():
    rows = _subagent_rows("aaa", attribution="acme-secret-reviewer") + _subagent_rows("bbb", attribution="Explore")
    events = convert_claude_code_transcript(rows, Names(Pseudonymizer(new_key()))).events
    agents = {e["agent"] for e in events}
    assert "claude-code-subagent:Explore" in agents
    custom = next(a for a in agents if a != "claude-code-subagent:Explore")
    assert custom.startswith("claude-code-subagent:h:")
    text = json.dumps(events)
    assert not any(form in text for form in canary_forms("acme-secret-reviewer"))
    assert all(not check_event(e) for e in events)


def test_a_subagent_without_a_type_is_named_by_a_pseudonym_of_its_id():
    events = convert_claude_code_transcript(_subagent_rows("a965ae36117666968"), Names(Pseudonymizer(new_key()))).events
    (agent,) = {e["agent"] for e in events}
    assert agent.startswith("claude-code-subagent:h:")
    assert "a965ae36117666968" not in json.dumps(events)


@pytest.mark.parametrize(
    ("agent", "clean"),
    [
        ("claude-code-subagent:Explore", True),
        ("claude-code-subagent:h:" + "0" * 64, True),
        ("claude-code-subagent", True),
        ("claude-code-subagent:acme-secret-reviewer", False),
        ("claude-code-subagent:", False),
        ("acme:Explore", False),
    ],
)
def test_lint_allows_only_built_in_or_pseudonymous_subagent_types(agent, clean):
    event = {"trace_id": "h:" + "0" * 64, "agent": agent}
    assert (check_event(event) == []) is clean
