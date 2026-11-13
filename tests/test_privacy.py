import json
import os
import socket

import pytest
from conftest import CANARIES, canary_forms

from profiler_client import cli
from profiler_client.claude_code import convert_claude_code_transcript
from profiler_client.dataset import EVENTS, PrepareError, prepare, read_sealed, sealed_payload, validate
from profiler_client.lint import check_event
from profiler_client.names import Names
from profiler_client.pseudonym import Pseudonymizer, load_or_create_key, new_key
from profiler_client.report import STATIC, view, write_name_list


def _events(rows, key=b"k" * 32):
    return convert_claude_code_transcript(rows, Names(Pseudonymizer(key))).events


def test_no_canary_reaches_the_dataset_in_any_form(tmp_path, transcript_file):
    out = tmp_path / "ready"
    prepare([transcript_file], out, Names(Pseudonymizer(new_key())))
    written = (out / EVENTS).read_text() + (out / "manifest.json").read_text()
    for canary in CANARIES:
        for form in canary_forms(canary):
            assert form not in written, f"a form of canary {canary!r} leaked"


def test_every_event_passes_the_lint(transcript_rows):
    for event in _events(transcript_rows):
        assert check_event(event) == []


def test_public_vocabulary_stays_readable_and_the_rest_is_pseudonymized(transcript_rows):
    nodes = [e["node"] for e in _events(transcript_rows) if e["event_type"] == "tool_call"]
    assert nodes[0] == "Bash: git status"
    assert nodes[1].startswith("Bash: python h:")
    assert nodes[2].startswith("mcp h:")
    assert nodes[3] == "Read: .py"
    assert nodes[4].startswith("Bash: make h:")  # a make target is told apart, never shown
    assert nodes[5].startswith("Bash: h:")


def test_equal_inputs_stay_equal_and_keys_separate_datasets(transcript_rows):
    status = [e for e in _events(transcript_rows) if e["node"] == "Bash: git status"]
    # the cosmetic description differs; the command doesn't
    assert status[0]["input_state"] == status[1]["input_state"]
    assert status[0]["result"] == status[1]["result"]
    other = [e for e in _events(transcript_rows, key=b"x" * 32) if e["node"] == "Bash: git status"]
    assert other[0]["input_state"] != status[0]["input_state"]


def test_completeness_and_custom_model(transcript_rows):
    events = _events(transcript_rows)
    assert {e["input_complete"] for e in events if e["event_type"] == "tool_call"} == {True}
    assert {e["input_complete"] for e in events if e["event_type"] == "decision"} == {False}
    models = {e["model"] for e in events if e["event_type"] == "decision"}
    assert "claude-opus-4-1" in models
    assert any(m.startswith("h:") for m in models)
    assert all("user_input" not in e for e in events)


def test_lint_names_the_field_not_the_value():
    event = {"trace_id": "h:" + "0" * 64, "run_id": "h:" + "0" * 64, "workflow": "claude-code/h:" + "0" * 64,
             "event_type": "tool_call", "node": "Bash: deploy_acme_prod", "user_input": "Acme Corp"}
    assert sorted(check_event(event)) == ["node", "user_input"]


def test_validate_catches_an_edited_payload(tmp_path, transcript_file):
    out = tmp_path / "ready"
    prepare([transcript_file], out, Names(Pseudonymizer(new_key())))
    validate(out)
    with (out / EVENTS).open("a") as f:
        f.write(json.dumps({"node": "x"}) + "\n")
    with pytest.raises(PrepareError, match="PAYLOAD_CHANGED"):
        validate(out)


def test_events_jsonl_carries_a_seal_that_checks_it_on_its_own(tmp_path, transcript_file):
    out = tmp_path / "ready"
    manifest = prepare([transcript_file], out, Names(Pseudonymizer(new_key())))
    payload = (out / EVENTS).read_bytes()
    seal, events = read_sealed(payload)
    assert seal["events"] == manifest["events"] == len(events)
    assert seal["contract_version"] == manifest["contract_version"]
    assert seal["policy_version"] == manifest["policy_version"]
    seal_line, body = payload.split(b"\n", 1)
    # no seal, an edited event, a dropped event: each is refused
    for broken, code in [
        (body, "SEAL_MISSING"),
        (seal_line + b"\n" + body.replace(b'"Bash"', b'"Bosh"', 1), "PAYLOAD_CHANGED"),
        (seal_line + b"\n" + body.split(b"\n", 1)[1], "PAYLOAD_CHANGED"),
        (b'{"profiler_client_seal": {}, "x": 1}\n' + body, "SEAL_MISSING"),
    ]:
        with pytest.raises(PrepareError, match=code):
            read_sealed(broken)


def test_a_resealed_dataset_still_has_to_pass_the_lint():
    # the seal is a checksum anyone can compute: the lint is what holds
    leaked = {"event_type": "tool_call", "agent": "main", "node": "customer list for Acme Corp"}
    with pytest.raises(PrepareError, match="PRIVACY_LINT") as exc:
        read_sealed(sealed_payload([leaked], frozenset()))
    assert "Acme" not in str(exc.value)


def test_symlinks_and_other_formats_are_refused(tmp_path, transcript_file):
    link = tmp_path / "link.jsonl"
    link.symlink_to(transcript_file)
    with pytest.raises(PrepareError, match="INPUT_SYMLINK"):
        prepare([link], tmp_path / "a", Names(Pseudonymizer(new_key())))
    other = tmp_path / "other.jsonl"
    other.write_text('{"trace_id": "t"}\n')
    with pytest.raises(PrepareError, match="FORMAT_UNSUPPORTED"):
        prepare([other], tmp_path / "b", Names(Pseudonymizer(new_key())))


def test_errors_never_quote_file_content(tmp_path, capsys):
    bad = tmp_path / "bad.jsonl"
    bad.write_text("ACME_TOKEN_9f8e7d not json\n")
    assert cli.main(["prepare", str(bad), "-o", str(tmp_path / "out")]) == 1
    assert "ACME_TOKEN_9f8e7d" not in capsys.readouterr().err


def test_key_file_is_private(tmp_path):
    path = tmp_path / "cfg" / "key"
    key = load_or_create_key(path)
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert load_or_create_key(path) == key
    os.chmod(path, 0o644)
    with pytest.raises(ValueError, match="chmod 600"):
        load_or_create_key(path)


def test_name_list_contains_names_but_never_inputs_outputs_or_ids(tmp_path):
    names = Names(Pseudonymizer(b"n" * 32))
    names.step("workflow-name")
    names.step("agent-name")
    names.step("tool-name")
    names.step("project-directory", domain="id")
    names.model("private-model")
    names.token("input", "private input")
    names.token("output", "private output")
    names.token("id", "private identifier")

    path = write_name_list("a" * 64, names.seen, tmp_path)
    stored = json.loads(path.read_text())
    assert stored["names"] == names.seen
    assert set(stored["names"].values()) == {
        "workflow-name", "agent-name", "tool-name", "private-model"
    }
    assert os.stat(path).st_mode & 0o777 == 0o600
    os.chmod(path, 0o644)
    write_name_list("a" * 64, names.seen, tmp_path)
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_prepare_writes_seal_hash_name_list_unless_disabled(tmp_path, transcript_file, monkeypatch):
    data_home = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    output = tmp_path / "ready"

    assert cli.main(["prepare", str(transcript_file), "-o", str(output)]) == 0
    manifest, _ = validate(output)
    seal, _ = read_sealed((output / EVENTS).read_bytes())
    name_path = data_home / "profiler-client" / "names" / f"{seal['payload_sha256']}.json"
    assert name_path.is_file()
    assert manifest["payload_sha256"] != seal["payload_sha256"]
    assert json.loads(name_path.read_text())["names"]
    assert os.stat(name_path).st_mode & 0o777 == 0o600

    no_names = tmp_path / "ready-without-names"
    assert cli.main(["prepare", str(transcript_file), "-o", str(no_names), "--no-name-list"]) == 0
    assert list(name_path.parent.iterdir()) == [name_path]


def test_open_restores_names_from_matching_payload_hash(tmp_path, monkeypatch):
    payload_hash = "b" * 64
    workflow_token = "h:" + "1" * 64
    node_token = "h:" + "2" * 64
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    names_dir = tmp_path / "profiler-client" / "names"
    write_name_list(payload_hash, {workflow_token: "billing-workflow", node_token: "calculate-total"}, names_dir)
    source = tmp_path / "report.json"
    source.write_text(json.dumps({
        "payload_sha256": payload_hash,
        "observe": {"selected_workflow": workflow_token, "workflows": [{"name": workflow_token, "num_runs": 1}]},
        "summary": {"totals": {"cost": 1, "runs": 1, "llm_calls": 0, "tool_calls": 1, "tokens": 0}},
        "mine": {"by_agent": {"agent": {"actions": [{"node": node_token, "occurrences": 1, "total_cost": 1}]}}},
    }))
    output = tmp_path / "report.html"

    assert cli.main(["open", str(source), "-o", str(output)]) == 0
    rendered = output.read_text()
    assert "billing-workflow" in rendered
    assert "calculate-total" in rendered
    assert workflow_token not in rendered
    assert node_token not in rendered
    # the decoded page holds real values, so it is owner-only
    assert (output.stat().st_mode & 0o777) == 0o600


def test_open_draws_the_report_with_the_shared_renderer(tmp_path, monkeypatch):
    # the page embeds report.js and report.css from the package, the same files the web view uses,
    # and report text can't close the data script early
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    source = tmp_path / "report.json"
    source.write_text(json.dumps({"summary": {"notes": "</script><script>alert(1)</script>"}, "observe": {}, "mine": {}}))
    output = tmp_path / "report.html"
    assert cli.main(["open", str(source), "-o", str(output)]) == 0
    rendered = output.read_text()
    assert (STATIC / "report.js").read_text() in rendered
    assert (STATIC / "report.css").read_text() in rendered
    assert "ProfilerReport.render(" in rendered
    assert "<script>alert(1)" not in rendered
    assert rendered.count("</script>") == 3  # the data, the renderer and the call: nothing from the report


def test_open_never_writes_a_findings_restored_inputs(tmp_path, monkeypatch):
    # a finding lists the inputs it matched; their pseudonyms are in the name list
    # (short event-log field values), but the page draws no input, so none is written
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    payload_hash, value_token, node_token = "b" * 64, "h:" + "3" * 64, "h:" + "2" * 64
    write_name_list(payload_hash, {value_token: "private-input", node_token: "calculate-total"},
                    tmp_path / "profiler-client" / "names")
    action = {"node": node_token, "occurrences": 2, "total_cost": 1, "classification": "INSUFFICIENT_EVIDENCE",
              "evidence": {"occurrences": 2, "inputs": [{"input": {"query": value_token}}]}}
    source = tmp_path / "report.json"
    source.write_text(json.dumps({
        "payload_sha256": payload_hash,
        "summary": {"totals": {"cost": 1}, "opportunities": [
            {"type": "CACHE_RESULT", "nodes": [node_token], "groups": [{"input": {"query": value_token}}]}]},
        "mine": {"by_agent": {"agent": {"actions": [action]}}},
    }))
    output = tmp_path / "report.html"
    assert cli.main(["open", str(source), "-o", str(output)]) == 0
    rendered = output.read_text()
    assert "calculate-total" in rendered
    assert "private-input" not in rendered and value_token not in rendered


def test_open_refuses_an_oversized_or_too_deeply_nested_report(tmp_path):
    big = tmp_path / "big.json"
    big.write_bytes(b'{"x":"' + b"a" * (64 * 1024 * 1024 + 1) + b'"}')
    assert cli.main(["open", str(big), "-o", str(tmp_path / "big.html")]) == 1

    deep = tmp_path / "deep.json"
    deep.write_bytes(b"[" * 200000 + b"]" * 200000)
    assert cli.main(["open", str(deep), "-o", str(tmp_path / "deep.html")]) == 1


def test_forget_removes_only_the_named_datasets_list(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    names_dir = tmp_path / "profiler-client" / "names"
    keep, drop = "a" * 64, "b" * 64
    write_name_list(keep, {"h:" + "1" * 64: "keep-me"}, names_dir)
    write_name_list(drop, {"h:" + "2" * 64: "drop-me"}, names_dir)
    # forget by a report JSON that names the dataset
    report = tmp_path / "r.json"
    report.write_text(json.dumps({"payload_sha256": drop}))
    assert cli.main(["forget", str(report)]) == 0
    assert not (names_dir / f"{drop}.json").exists()
    assert (names_dir / f"{keep}.json").exists()
    # --all clears the rest
    assert cli.main(["forget", "--all"]) == 0
    assert list(names_dir.glob("*.json")) == []


def test_open_without_a_hash_never_borrows_another_datasets_names(tmp_path, monkeypatch):
    # a name list exists for one dataset; a report that carries no hash (or a
    # different one) must not be decoded against it -- the old load-everything leak
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    names_dir = tmp_path / "profiler-client" / "names"
    other_token = "h:" + "1" * 64
    write_name_list("b" * 64, {other_token: "billing-workflow"}, names_dir)

    foreign_token = "h:" + "9" * 64
    report = {
        "observe": {"selected_workflow": foreign_token, "workflows": [{"name": foreign_token, "num_runs": 1}]},
        "summary": {"totals": {"cost": 1, "runs": 1, "llm_calls": 0, "tool_calls": 1, "tokens": 0}},
        "mine": {"by_agent": {"agent": {"actions": [{"node": other_token, "occurrences": 1, "total_cost": 1}]}}},
    }
    for payload in (report, {**report, "payload_sha256": "c" * 64}):  # no hash, and a non-matching hash
        source = tmp_path / "report.json"
        source.write_text(json.dumps(payload))
        output = tmp_path / "report.html"
        assert cli.main(["open", str(source), "-o", str(output)]) == 0
        rendered = output.read_text()
        assert "billing-workflow" not in rendered  # the other dataset's name never leaks in
        assert other_token[2:9] in rendered  # the pseudonym is shown instead


def test_runs_with_the_network_disabled(tmp_path, transcript_file, monkeypatch, capsys):
    def no_network(*args, **kwargs):
        raise AssertionError("profiler-client tried to open a socket")

    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    out = tmp_path / "ready"
    assert cli.main(["prepare", str(transcript_file), "-o", str(out)]) == 0
    assert cli.main(["validate", str(out)]) == 0
    assert cli.main(["inspect", str(out)]) == 0
    assert cli.main(["version"]) == 0
    # open is local too: a downloaded report is decoded with no network
    report = out.with_suffix(".report.json")
    report.write_text(json.dumps({"summary": {"totals": {}}, "observe": {}, "mine": {}}))
    assert cli.main(["open", str(report), "-o", str(tmp_path / "r.html")]) == 0
    # and so is show-event
    seal, events = read_sealed((out / EVENTS).read_bytes())
    report.write_text(json.dumps({"payload_sha256": seal["payload_sha256"]}))
    assert cli.main(["show-event", str(report), events[0]["event_id"]]) == 0


def test_lint_takes_an_input_field_only_as_a_pseudonym_under_a_pseudonym_or_kept_name():
    t = "h:" + "0" * 64
    base = {"trace_id": t, "run_id": t, "workflow": t, "event_type": "tool_call", "node": t}
    assert check_event({**base, "input_state": {t: t}}) == []
    assert check_event({**base, "input_state": {"band": t}}, kept={"band"}) == []
    assert check_event({**base, "input_state": {"band": t}}) == ["input_state.band"]
    assert check_event({**base, "input_state": {t: "<1%"}}) == [f"input_state.{t}"]


def test_the_seal_names_the_key_so_datasets_sharing_it_can_be_combined(tmp_path, transcript_file):
    key_file = tmp_path / "key"
    ids = []
    for out, key in (("a", ["--key-file", str(key_file)]), ("b", ["--key-file", str(key_file)]), ("c", [])):
        assert cli.main(["prepare", str(transcript_file), "-o", str(tmp_path / out), "--no-name-list", *key]) == 0
        seal, _ = read_sealed((tmp_path / out / EVENTS).read_bytes())
        manifest, _ = validate(tmp_path / out)
        assert seal["key_id"] == manifest["key_id"]
        ids.append(seal["key_id"])
    # the same key file gives the same id; a fresh key a different one
    assert ids[0] == ids[1] != ids[2]
    # the id doesn't reveal the key
    assert ids[0] not in key_file.read_text()
    bad = sealed_payload([], frozenset(), "NOT-HEX")
    with pytest.raises(PrepareError, match="key_id"):
        read_sealed(bad)


def test_open_restores_names_from_every_dataset_of_a_combined_report(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    names_dir = tmp_path / "profiler-client" / "names"
    first, second, absent = "a" * 64, "b" * 64, "c" * 64
    write_name_list(first, {"h:" + "1" * 64: "billing-workflow"}, names_dir)
    write_name_list(second, {"h:" + "2" * 64: "calculate-total"}, names_dir)
    source = tmp_path / "report.json"
    source.write_text(json.dumps({
        "payload_sha256": first,
        "payload_sha256s": [first, second, absent],
        "observe": {"selected_workflow": "h:" + "1" * 64, "workflows": [{"name": "h:" + "1" * 64, "num_runs": 1}]},
        "mine": {"by_agent": {"agent": {"actions": [{"node": "h:" + "2" * 64, "occurrences": 1, "total_cost": 1}]}}},
    }))
    assert cli.main(["open", str(source), "-o", str(tmp_path / "report.html")]) == 0
    rendered = (tmp_path / "report.html").read_text()
    assert "billing-workflow" in rendered and "calculate-total" in rendered
    assert f"no name list on this machine for dataset {absent[:12]}" in capsys.readouterr().out
    assert cli.main(["forget", str(source)]) == 0
    assert list(names_dir.glob("*.json")) == []


# --llm-inputs: one pseudonym of everything the model read, per LLM call

def _llm_inputs(rows, key=b"k" * 32):
    events = convert_claude_code_transcript(rows, Names(Pseudonymizer(key), llm_inputs=True)).events
    return [e for e in events if e["event_type"] == "decision"]


def _rerun(rows, suffix):
    """The same conversation in another session: new session, message and tool ids, later timestamps."""
    text = json.dumps(rows)
    for old in ("sess-acme-secret-project-1", '"m1"', '"m2"', '"m3"', '"p1"'):
        text = text.replace(old, old[:-1] + suffix + old[-1] if old.startswith('"') else old + suffix)
    for i in range(1, 8):
        text = text.replace(f'"t{i}"', f'"t{i}{suffix}"')
    return [{**r, "timestamp": "2026-10-02" + r["timestamp"][10:]} if "timestamp" in r else r
            for r in json.loads(text)]


def test_llm_inputs_are_off_by_default(transcript_rows):
    assert all("input" not in e["input_state"] for e in _events(transcript_rows) if e["event_type"] == "decision")


def test_llm_inputs_are_complete_pseudonyms_that_pass_the_lint(tmp_path, transcript_file):
    calls = _llm_inputs([json.loads(line) for line in transcript_file.read_text().splitlines()])
    assert all(e["input_state"]["input"].startswith("h:") and e["input_complete"] for e in calls)
    assert len({e["input_state"]["input"] for e in calls}) == len(calls)  # each call read more than the one before
    out = tmp_path / "ready"
    manifest = prepare([transcript_file], out, Names(Pseudonymizer(new_key()), llm_inputs=True))
    assert manifest["llm_inputs"] is True
    written = (out / EVENTS).read_text() + (out / "manifest.json").read_text()
    for canary in CANARIES:
        for form in canary_forms(canary):
            assert form not in written, f"a form of canary {canary!r} leaked"
    validate(out)


def test_the_same_conversation_has_the_same_llm_inputs_whatever_its_ids(transcript_rows):
    first, again = _llm_inputs(transcript_rows), _llm_inputs(_rerun(transcript_rows, "b"))
    assert [e["input_state"] for e in again] == [e["input_state"] for e in first]
    assert [e["input_state"] for e in _llm_inputs(transcript_rows, key=b"x" * 32)] != [e["input_state"] for e in first]


def test_a_different_tool_output_changes_every_later_llm_input(transcript_rows):
    changed = [dict(r) for r in transcript_rows]
    changed[2] = {**changed[2], "message": {"content": [
        {**c, "content": "On branch dev"} if c["tool_use_id"] == "t1" else c for c in changed[2]["message"]["content"]]}}
    first, other = _llm_inputs(transcript_rows), _llm_inputs(changed)
    assert first[0]["input_state"] == other[0]["input_state"]  # read before the output
    assert all(a["input_state"] != b["input_state"] for a, b in zip(first[1:], other[1:]))


def test_open_keeps_only_the_event_ids_a_map_step_can_look_up(tmp_path, monkeypatch):
    # show-event needs the ids; the run ids and anything that isn't an event id stay out of the page
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    event_id, run_id = "h:" + "4" * 64, "h:" + "5" * 64
    sample = [{"event_id": event_id, "run_id": run_id}, {"event_id": "not an id <b>"}]
    source = tmp_path / "it's a report.json"
    source.write_text(json.dumps({"graph": {"run_count": 1, "nodes": [], "edges": [
        {"from": "@prompt", "to": "step", "kind": "starts", "transition_count": 1, "evidence_sample": sample}]}}))
    output = tmp_path / "report.html"
    assert cli.main(["open", str(source), "-o", str(output)]) == 0
    rendered = output.read_text()
    assert event_id in rendered and str(source.resolve()) in rendered
    assert run_id not in rendered and "not an id" not in rendered


def test_open_keeps_the_headline_and_next_steps_but_no_input_slice(tmp_path, monkeypatch):
    # "do these first" names steps (restored on this machine); a region's slice is an input value, never written
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    payload_hash, node_token, value_token = "b" * 64, "h:" + "2" * 64, "h:" + "3" * 64
    write_name_list(payload_hash, {node_token: "calculate-total", value_token: "private-input"},
                    tmp_path / "profiler-client" / "names")
    step = {"step": "review", "do": "Check it.", "type": "DETERMINISTIC_REGION", "title": node_token, "nodes": [node_token],
            "where": f"severity={value_token}", "saving": 1.5, "risk": "medium", "risk_reasons": ["12 calls in the slice"],
            "observations": 12, "group": 0, "workflow": "wf"}
    source = tmp_path / "report.json"
    source.write_text(json.dumps({"payload_sha256": payload_hash, "summary": {
        "totals": {"cost": 4}, "replaceable": {"cost": 1.5, "findings": 1},
        "risk_labels": {"medium": "Needs review"}, "next_steps": [step]}}))
    page = view(json.loads(source.read_text()))["summary"]
    assert page["replaceable"] == {"cost": 1.5, "findings": 1} and page["risk_labels"] == {"medium": "Needs review"}
    assert set(page["next_steps"][0]) == set(step) - {"where", "group", "workflow"}
    output = tmp_path / "report.html"
    assert cli.main(["open", str(source), "-o", str(output)]) == 0
    rendered = output.read_text()
    assert "calculate-total" in rendered and node_token not in rendered
    assert "private-input" not in rendered and value_token not in rendered


def test_a_report_without_next_steps_draws_no_cards():
    # an older or diagnose-mode report: the renderer hides "do these first" when next_steps is absent
    assert "next_steps" not in view({"summary": {"totals": {"cost": 1}}})["summary"]
    assert view({"summary": {"next_steps": []}})["summary"]["next_steps"] == []


def test_open_keeps_an_overviews_rows_and_each_workflows_report_with_names_restored(tmp_path, monkeypatch):
    # a dataset overview: rows and reports keyed by workflow name, restored alike; a report's input slices stay out
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    payload_hash, wf_token, value_token = "c" * 64, "h:" + "6" * 64, "h:" + "7" * 64
    write_name_list(payload_hash, {wf_token: "fraud-screening", value_token: "private-input"},
                    tmp_path / "profiler-client" / "names")
    row = {"name": wf_token, "runs": 3, "spend": 2.0, "replaceable": 0.5, "share": 0.25, "findings": 1,
           "highest_risk": "medium", "where": f"x={value_token}"}
    report = {"summary": {"totals": {"cost": 2.0}, "next_steps": [{"step": "review", "where": f"x={value_token}"}]},
              "observe": {"selected_workflow": wf_token, "workflows": [{"name": wf_token, "num_runs": 3}]}}
    source = tmp_path / "overview.json"
    source.write_text(json.dumps({"payload_sha256": payload_hash, "grade": {"grade": "B", "word": "Good"},
                                  "overview": {"workflows": [row], "totals": {"spend": 2.0, "replaceable": 0.5},
                                               "risk_labels": {"medium": "Needs review"}},
                                  "reports": {wf_token: report}}))
    page = view(json.loads(source.read_text()))
    assert set(page) == {"overview", "reports", "grade"}
    assert set(page["overview"]["workflows"][0]) == set(row) - {"where"}
    assert page["reports"][wf_token]["observe"]["selected_workflow"] == wf_token
    assert "where" not in page["reports"][wf_token]["summary"]["next_steps"][0]
    output = tmp_path / "overview.html"
    assert cli.main(["open", str(source), "-o", str(output)]) == 0
    rendered = output.read_text()
    assert rendered.count("fraud-screening") >= 3 and wf_token not in rendered
    assert "private-input" not in rendered and value_token not in rendered
