"""Codex, OpenTelemetry, OpenAI chat and generic event logs: the same
privacy guarantees as Claude Code transcripts, and ``--keep-names``."""
import hashlib
import json

import pytest
from conftest import CANARIES, canary_forms

from profiler_client import cli
from profiler_client.dataset import EVENTS, MANIFEST, PrepareError, prepare, read_sealed, validate
from profiler_client.lint import check_event
from profiler_client.names import Names
from profiler_client.pseudonym import Pseudonymizer, new_key

CWD = "/home/alice/acme-secret-project"


def _codex_rows():
    def item(payload, ts="2026-10-01T10:00:00.000Z"):
        return {"timestamp": ts, "type": "response_item", "payload": payload}

    def call(call_id, name, args, ts, namespace=None):
        payload = {"type": "function_call", "call_id": call_id, "name": name, "arguments": json.dumps(args)}
        if namespace:
            payload["namespace"] = namespace
        return item(payload, ts)

    def output(call_id, text, ts):
        return item({"type": "function_call_output", "call_id": call_id, "output": text}, ts)

    def shell_out(chunk, code, body):
        return f"Chunk ID: {chunk}\nWall time: 0.{chunk} seconds\nProcess exited with code {code}\n" \
               f"Original token count: 9\nOutput:\n{body}"

    tokens = {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 1000, "output_tokens": 50}}}
    return [
        {"timestamp": "2026-10-01T10:00:00.000Z", "type": "session_meta",
         "payload": {"id": "sess-acme-secret-project-1", "cwd": CWD, "cli_version": "0.50.0",
                     "originator": "codex_cli_rs"}},
        {"type": "turn_context", "payload": {"turn_id": "turn-1", "model": "gpt-5.5", "cwd": CWD}},
        item({"type": "message", "role": "user",
              "content": [{"type": "input_text", "text": "customer list for Acme Corp, ACME_TOKEN_9f8e7d"}]}),
        item({"type": "reasoning", "summary": [], "encrypted_content": "acme-secret-project"}),
        call("c1", "exec_command", {"cmd": "git status", "workdir": CWD, "justification": "acme"},
             "2026-10-01T10:00:01.000Z"),
        call("c2", "exec_command", {"cmd": "python deploy_acme_prod.py", "workdir": CWD}, "2026-10-01T10:00:01.000Z"),
        call("c3", "_lookup", {"customer": "Acme Corp"}, "2026-10-01T10:00:01.000Z",
             namespace="mcp__acme_internal"),
        output("c1", shell_out(111, 0, "On branch main"), "2026-10-01T10:00:02.000Z"),
        output("c2", shell_out(222, 1, "deployed acme-secret-project"), "2026-10-01T10:00:03.000Z"),
        output("c3", "Wall time: 0.1 seconds\nOutput:\ncustomer list for Acme Corp", "2026-10-01T10:00:03.000Z"),
        {"type": "event_msg", "payload": tokens},
        item({"type": "custom_tool_call", "call_id": "c4", "name": "apply_patch",
              "input": f"*** Begin Patch\n*** Update File: {CWD}/src/billing.py\n+ACME_TOKEN_9f8e7d\n*** End Patch"},
             "2026-10-01T10:00:04.000Z"),
        call("c5", "exec_command", {"cmd": "git status", "workdir": CWD, "justification": "again"},
             "2026-10-01T10:00:04.000Z"),
        call("c6", "exec_command", {"cmd": "corp_cli sync --token ACME_TOKEN_9f8e7d", "workdir": CWD},
             "2026-10-01T10:00:04.000Z"),
        item({"type": "custom_tool_call_output", "call_id": "c4",
              "output": [{"type": "input_text", "text": "Exit code: 0\nWall time: 1 seconds\nOutput:\nok"}]},
             "2026-10-01T10:00:05.000Z"),
        output("c5", shell_out(333, 0, "On branch main"), "2026-10-01T10:00:05.000Z"),
        output("c6", shell_out(444, 0, "ok"), "2026-10-01T10:00:05.000Z"),
        {"type": "event_msg", "payload": tokens},
        item({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Done, Acme Corp"}]},
             "2026-10-01T10:00:07.000Z"),
        {"type": "event_msg", "payload": tokens},
    ]


def _chat_rows():
    return [{
        "id": "traj-acme-secret-project", "benchmark": "acme-secret-project", "model": "acme-finetune-v3",
        "messages": [
            {"role": "system", "content": "You help Acme Corp"},
            {"role": "user", "content": "customer list for Acme Corp"},
            {"role": "assistant", "content": None, "finish_reason": "tool_calls",
             "usage": {"prompt_tokens": 300, "completion_tokens": 20},
             "tool_calls": [
                 {"id": "a", "type": "function",
                  "function": {"name": "corp_cli", "arguments": json.dumps({"command": "git status"})}},
                 {"id": "b", "type": "function",
                  "function": {"name": "mcp__acme_internal__lookup",
                               "arguments": json.dumps({"customer": "Acme Corp"})}},
             ]},
            {"role": "tool", "tool_call_id": "b", "content": "ACME_TOKEN_9f8e7d"},
            {"role": "tool", "tool_call_id": "a", "content": "On branch main"},
            {"role": "assistant", "content": "Done for Acme Corp", "finish_reason": "stop"},
        ],
    }]


def _attr(key, value):
    kind = "intValue" if isinstance(value, int) else "stringValue"
    return {"key": key, "value": {kind: str(value) if kind == "intValue" else value}}


def _otel_rows():
    def span(span_id, start, end, attrs, name="span", error=False):
        return {"traceId": "trace-acme-1", "spanId": span_id, "parentSpanId": "root-acme", "name": name,
                "startTimeUnixNano": str(start), "endTimeUnixNano": str(end),
                "attributes": [_attr(k, v) for k, v in attrs.items()],
                "status": {"code": 2, "message": "ACME_TOKEN_9f8e7d rejected"} if error else {}}

    spans = [
        span("s0", 0, 10, {"openinference.span.kind": "CHAIN"}, name="acme-secret-project"),
        span("s1", 1_000_000, 2_000_000, {
            "gen_ai.operation.name": "chat", "gen_ai.request.model": "acme-finetune-v3",
            "gen_ai.usage.input_tokens": 500, "gen_ai.usage.output_tokens": 40,
            "gen_ai.agent.name": "acme-secret-project"}),
        span("s2", 3_000_000, 5_000_000, {
            "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "deploy_acme_prod.py",
            "gen_ai.tool.call.arguments": json.dumps({"customer": "Acme Corp"}),
            "gen_ai.tool.call.result": "customer list for Acme Corp"}),
        span("s3", 6_000_000, 7_000_000, {"openinference.span.kind": "TOOL", "tool.name": "corp_cli"}, error=True),
        span("s4", 8_000_000, 9_000_000, {"gen_ai.operation.name": "chat", "gen_ai.response.model": "gpt-5.5",
                                         "gen_ai.response.finish_reasons": "stop"}),
    ]
    return [{"resourceSpans": [{"resource": {"attributes": [_attr("service.name", "acme-secret-project")]},
                                "scopeSpans": [{"spans": spans}]}]}]


def _event_rows():
    common = {"traceId": "acme-trace-1", "runId": "acme-run-1", "workflow_name": "acme-secret-project",
              "agent": "acme-agent"}
    return {"events": [
        {**common, "type": "llm", "name": "plan for Acme Corp", "model": "acme-finetune-v3",
         "prompt_tokens": 120, "completion_tokens": 10, "cost_usd": 0.002, "user_input": "customer list for Acme Corp",
         "decision": {"type": "next_action", "selected": "corp_cli"}},
        {**common, "type": "tool_call", "name": "corp_cli", "tool_name": "corp_cli", "duration_ms": 30,
         "input_state": {"cmd": "make deploy-acme", "token": "ACME_TOKEN_9f8e7d"},
         "output": "deployed acme-secret-project", "status": "failed", "invalidation_keys": ["acme-secret-project"],
         "cacheable": True, "trigger": "user_prompt"},
    ]}


def _write(tmp_path, name, rows, document=False):
    path = tmp_path / name
    if document:
        path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    else:
        path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


FORMATS = {
    "codex": lambda tmp: _write(tmp, "rollout.jsonl", _codex_rows()),
    "openai-chat": lambda tmp: _write(tmp, "chat.jsonl", _chat_rows()),
    "otel": lambda tmp: _write(tmp, "spans.json", _otel_rows()[0], document=True),
    "events": lambda tmp: _write(tmp, "events.json", _event_rows(), document=True),
}


def _prepare(tmp_path, source, keep=False):
    out = tmp_path / "ready"
    manifest = prepare([FORMATS[source](tmp_path)], out, Names(Pseudonymizer(new_key()), keep=keep))
    events = [json.loads(line) for line in (out / EVENTS).read_text().splitlines()[1:]]  # after the seal
    return out, manifest, events


@pytest.mark.parametrize("source", FORMATS)
def test_no_canary_reaches_the_dataset_in_any_form(tmp_path, source):
    out, manifest, events = _prepare(tmp_path, source)
    assert manifest["source_format"] == source
    assert manifest["kept_names"] == []
    assert events
    written = (out / EVENTS).read_text() + (out / MANIFEST).read_text()
    for canary in CANARIES:
        for form in canary_forms(canary):
            assert form not in written, f"a form of canary {canary!r} leaked from {source}"
    for event in events:
        assert check_event(event) == []
    validate(out)


def test_codex_nodes_tokens_and_results(tmp_path):
    _, manifest, events = _prepare(tmp_path, "codex")
    assert manifest["sessions_without_cost"] == 1
    tools = [e for e in events if e["event_type"] == "tool_call"]
    nodes = [e["node"] for e in tools]
    assert nodes[0] == "exec_command: git status"
    assert nodes[1].startswith("exec_command: python h:")
    assert nodes[2].startswith("mcp h:")
    assert nodes[3] == "apply_patch: .py"
    assert nodes[5].startswith("exec_command: h:")
    status = [e for e in tools if e["node"] == "exec_command: git status"]
    # different chunk ids, wall times and justifications: still the same input and output
    assert status[0]["input_state"] == status[1]["input_state"]
    assert status[0]["result"] == status[1]["result"]
    assert tools[1]["result"].startswith("error:")
    llms = [e for e in events if e["event_type"] == "decision"]
    assert [e["node"] for e in llms] == ["llm_call ← user_prompt", "llm_call ← exec_command", "llm_call ← apply_patch"]
    assert llms[0]["tokens"] == {"input": 1000, "output": 50}
    assert llms[0]["model"] == "gpt-5.5"
    assert llms[2]["decision"]["selected"] == "respond"
    assert llms[1]["trigger"].count(" + ") == 2 and "(error)" in llms[1]["trigger"]
    assert {e["workflow_version"] for e in events} == {"0.50.0"}


def test_chat_pairs_outputs_by_id(tmp_path):
    _, _, events = _prepare(tmp_path, "openai-chat")
    llm, shell, lookup, final = events
    assert shell["node"].endswith(": git status") and shell["node"].startswith("h:")
    assert lookup["node"].startswith("mcp h:")
    assert shell["result"] != lookup["result"]
    assert llm["tokens"] == {"input": 300, "output": 20} and llm["result"] == "tool_calls"
    assert llm["model"].startswith("h:")
    assert final["result"] == "stop" and final["decision"]["selected"] == "respond"


def test_otel_reads_genai_and_openinference_spans(tmp_path):
    _, manifest, events = _prepare(tmp_path, "otel")
    assert [e["event_type"] for e in events] == ["decision", "tool_call", "tool_call", "decision"]
    llm, tool, failed, final = events
    assert llm["tokens"] == {"input": 500, "output": 40} and llm["latency_ms"] == 1.0
    assert tool["input_complete"] is True and tool["result"].startswith("success:")
    assert failed["input_complete"] is False and failed["result"].startswith("error:")
    assert final["model"] == "gpt-5.5" and final["result"] == "stop"
    assert " (error)" in final["trigger"] and " + " in final["trigger"]
    assert manifest["sessions"] == 1 and manifest["sessions_without_cost"] == 1


def test_event_log_aliases_and_dropped_fields(tmp_path):
    _, manifest, events = _prepare(tmp_path, "events")
    llm, tool = events
    assert llm["event_type"] == "decision" and tool["event_type"] == "tool_call"
    assert llm["tokens"] == {"input": 120, "output": 10} and llm["cost"] == 0.002
    assert tool["latency_ms"] == 30 and tool["cache_eligible"] is True
    assert tool["result"].startswith("error:") and tool["trigger"] == "user_prompt"
    assert "user_input" not in llm and "invalidation_keys" not in tool
    assert manifest["sessions_without_cost"] == 0


def test_keep_names_keeps_single_word_names_only_and_lists_them(tmp_path):
    out, manifest, events = _prepare(tmp_path, "events", keep=True)
    assert manifest["kept_names"] == ["acme-agent", "acme-secret-project", "cmd", "corp_cli", "token"]
    llm, tool = events
    assert tool["node"] == "corp_cli" and tool["tool"] == {"name": "corp_cli"}
    assert llm["workflow"] == "acme-secret-project" and llm["decision"]["selected"] == "corp_cli"
    assert llm["node"].startswith("h:")  # a sentence is never kept
    written = (out / EVENTS).read_text()
    for canary in ("ACME_TOKEN_9f8e7d", "customer list for Acme Corp", "make deploy-acme", "acme-finetune-v3"):
        assert canary not in written
    validate(out)
    # the manifest must agree with the seal
    manifest_file = out / MANIFEST
    edited = json.loads(manifest_file.read_text())
    edited["kept_names"] = ["acme-agent"]
    manifest_file.write_text(json.dumps(edited))
    with pytest.raises(PrepareError, match="MANIFEST_INVALID"):
        validate(out)
    # a kept name the seal doesn't list fails the lint, even with a recomputed hash
    lines = (out / EVENTS).read_bytes().split(b"\n", 1)
    seal = json.loads(lines[0])
    seal["profiler_client_seal"]["kept_names"] = ["acme-agent"]
    with pytest.raises(PrepareError, match="PRIVACY_LINT"):
        read_sealed(json.dumps(seal).encode() + b"\n" + lines[1])


def test_keep_names_never_keeps_contents_in_codex(tmp_path):
    out, manifest, _ = _prepare(tmp_path, "codex", keep=True)
    assert manifest["kept_names"] == ["acme-secret-project", "mcp__acme_internal__lookup"]
    written = (out / EVENTS).read_text()
    for canary in ("deploy_acme_prod.py", "corp_cli", "ACME_TOKEN_9f8e7d", "customer list for Acme Corp"):
        assert canary not in written


def test_cli_prints_the_format_and_kept_names(tmp_path, capsys):
    path = FORMATS["events"](tmp_path)
    assert cli.main(["prepare", str(path), "-o", str(tmp_path / "out"), "--keep-names"]) == 0
    err = capsys.readouterr().err
    assert "(events)" in err and "5 name(s) kept in clear text" in err


def test_a_document_that_is_no_trace_format_is_refused(tmp_path):
    path = tmp_path / "other.json"
    path.write_text(json.dumps({"rows": [{"trace_id": "t"}]}, indent=2))
    with pytest.raises(PrepareError, match="FORMAT_UNSUPPORTED"):
        prepare([path], tmp_path / "out", Names(Pseudonymizer(new_key())))


def test_an_event_log_input_is_one_pseudonym_per_field(tmp_path):
    _, _, events = _prepare(tmp_path, "events")
    (tool,) = [e for e in events if e["event_type"] == "tool_call"]
    assert len(tool["input_state"]) == 2  # cmd and token, names and values pseudonyms
    assert all(k.startswith("h:") and v.startswith("h:") for k, v in tool["input_state"].items())
    (tmp_path / "kept").mkdir()
    _, manifest, kept = _prepare(tmp_path / "kept", "events", keep=True)
    (tool,) = [e for e in kept if e["event_type"] == "tool_call"]
    assert set(tool["input_state"]) == {"cmd", "token"} and {"cmd", "token"} <= set(manifest["kept_names"])
    assert all(v.startswith("h:") for v in tool["input_state"].values())


def test_the_same_value_in_two_fields_does_not_match():
    names = Names(Pseudonymizer(new_key()))
    fields = names.input_fields({"from": "Lisbon", "to": "Lisbon"})
    assert len(set(fields.values())) == 2
    assert names.input_fields({"to": "Lisbon"}) == {k: v for k, v in fields.items() if k == names.step("to")}


def test_only_fields_with_few_short_values_are_remembered():
    names = Names(Pseudonymizer(new_key()))
    for i in range(30):
        names.input_fields({"band": ["<1%", "1-3%", ">3%"][i % 3], "batch_id": f"B-{i}", "flag": i % 2 == 0,
                            "note": "x" * 50, "items": [i]})
    assert sorted(names.field_values().values()) == ["1-3%", "<1%", ">3%", "false", "true"]


@pytest.mark.parametrize("source", ["codex", "openai-chat"])
def test_llm_inputs_match_across_runs_with_new_ids_and_never_leak(tmp_path, source):
    def calls(rows, name):
        out = tmp_path / name
        prepare([_write(tmp_path, f"{name}.jsonl", rows)], out, Names(Pseudonymizer(b"k" * 32), llm_inputs=True))
        written = (out / EVENTS).read_text()
        for canary in CANARIES:
            for form in canary_forms(canary):
                assert form not in written, f"a form of canary {canary!r} leaked from {source}"
        events = [json.loads(line) for line in written.splitlines()[1:]]
        return [e for e in events if e["event_type"] == "decision"]

    rows = _codex_rows() if source == "codex" else _chat_rows()
    # another run of the same conversation: only ids differ
    text = json.dumps(rows).replace("sess-acme-secret-project-1", "sess-2").replace("traj-acme-secret-project", "traj-2")
    for old in ("c1", "c2", "c3", "c4", "c5", "c6", '"a"', '"b"'):
        text = text.replace(f'"{old}"' if not old.startswith('"') else old,
                            f'"{old}x"' if not old.startswith('"') else old[:-1] + 'x"')
    first, again = calls(rows, "one"), calls(json.loads(text), "two")
    assert all(e["input_complete"] and e["input_state"]["input"].startswith("h:") for e in first)
    assert len({e["input_state"]["input"] for e in first}) == len(first)
    assert [e["input_state"] for e in again] == [e["input_state"] for e in first]


ALL_FORMATS = {**FORMATS, "claude-code": None}


def _prepare_any(tmp_path, source, transcript_file):
    if source == "claude-code":
        out = tmp_path / "ready"
        manifest = prepare([transcript_file], out, Names(Pseudonymizer(new_key())))
        return out, manifest, [json.loads(line) for line in (out / EVENTS).read_text().splitlines()[1:]]
    return _prepare(tmp_path, source)


@pytest.mark.parametrize("source", ALL_FORMATS)
def test_every_event_has_its_own_pseudonymous_id(tmp_path, source, transcript_file):
    out, manifest, events = _prepare_any(tmp_path, source, transcript_file)
    ids = [e.get("event_id") for e in events]
    assert all(isinstance(i, str) and i.startswith("h:") for i in ids)
    assert len(set(ids)) == len(ids)
    seal, _ = read_sealed((out / EVENTS).read_bytes())
    assert seal["contract_version"] == manifest["contract_version"] == "2"
    assert seal["parent_relation"] == manifest["parent_relation"]
    assert seal["parent_relation"] == ("unspecified" if source == "events" else "invokes")


@pytest.mark.parametrize("source", ["claude-code", "codex", "openai-chat"])
def test_a_tool_calls_parent_is_the_llm_call_that_chose_it(tmp_path, source, transcript_file):
    _, _, events = _prepare_any(tmp_path, source, transcript_file)
    by_id = {e["event_id"]: e for e in events}
    tools = [e for e in events if e["event_type"] == "tool_call"]
    assert tools
    for tool in tools:
        parent = by_id[tool["parent"]]
        assert parent["event_type"] == "decision"
        assert parent["run_id"] == tool["run_id"]
        assert tool["node"] in parent["decision"]["selected"].split("+")


def test_otel_ids_are_span_ids_in_the_parents_id_space(tmp_path):
    rows = _otel_rows()[0]
    spans = rows["resourceSpans"][0]["scopeSpans"][0]["spans"]
    spans[2]["parentSpanId"] = "s1"  # the tool span runs inside the LLM span
    path = tmp_path / "spans.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    prepare([path], tmp_path / "ready", Names(Pseudonymizer(new_key())))
    events = [json.loads(line) for line in (tmp_path / "ready" / EVENTS).read_text().splitlines()[1:]]
    llm = next(e for e in events if e["event_type"] == "decision")
    assert next(e for e in events if e.get("parent") == llm["event_id"])["event_type"] == "tool_call"


def test_otel_tools_under_a_skipped_span_link_to_the_llm_call_that_chose_them(tmp_path):
    # every span's parent is an agent/chain span the client skips, as most GenAI instrumentations emit
    path = _write(tmp_path, "spans.json", _otel_rows()[0], document=True)
    prepare([path], tmp_path / "ready", Names(Pseudonymizer(new_key())))
    events = [json.loads(line) for line in (tmp_path / "ready" / EVENTS).read_text().splitlines()[1:]]
    first_llm = next(e for e in events if e["event_type"] == "decision")
    tools = [e for e in events if e["event_type"] == "tool_call"]
    assert len(tools) == 2 and all(t["parent"] == first_llm["event_id"] for t in tools)
    assert all(e["parent"] != first_llm["event_id"] for e in events if e["event_type"] == "decision")


def test_an_event_log_keeps_its_own_ids_and_parents_as_pseudonyms(tmp_path):
    rows = _event_rows()
    rows["events"][0]["id"] = "acme-evt-1"
    rows["events"][1]["parent"] = "acme-evt-1"
    path = _write(tmp_path, "events.json", rows, document=True)
    prepare([path], tmp_path / "ready", Names(Pseudonymizer(new_key())))
    events = [json.loads(line) for line in (tmp_path / "ready" / EVENTS).read_text().splitlines()[1:]]
    assert events[1]["parent"] == events[0]["event_id"]
    assert "acme-evt-1" not in (tmp_path / "ready" / EVENTS).read_text()


def test_contract_v1_datasets_are_still_read_and_a_bad_relation_is_refused(tmp_path):
    out, _, _ = _prepare(tmp_path, "codex")
    seal_line, body = (out / EVENTS).read_bytes().split(b"\n", 1)
    seal = json.loads(seal_line)["profiler_client_seal"]
    v1 = {k: v for k, v in seal.items() if k != "parent_relation"} | {"contract_version": "1"}
    events = [{k: v for k, v in json.loads(line).items() if k != "event_id"} for line in body.splitlines()]
    v1_body = b"".join(json.dumps(e, sort_keys=True).encode() + b"\n" for e in events)
    v1["payload_sha256"] = hashlib.sha256(v1_body).hexdigest()
    read_sealed(json.dumps({"profiler_client_seal": v1}).encode() + b"\n" + v1_body)
    bad = {**seal, "parent_relation": "follows"}
    with pytest.raises(PrepareError, match="SEAL_INVALID"):
        read_sealed(json.dumps({"profiler_client_seal": bad}).encode() + b"\n" + body)
    with pytest.raises(PrepareError, match="SEAL_UNSUPPORTED"):
        read_sealed(json.dumps({"profiler_client_seal": {**seal, "contract_version": "3"}}).encode() + b"\n" + body)
