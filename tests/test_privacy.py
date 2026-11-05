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
from profiler_client.report import write_name_list


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


def test_runs_with_the_network_disabled(tmp_path, transcript_file, monkeypatch, capsys):
    def no_network(*args, **kwargs):
        raise AssertionError("profiler-client tried to open a socket")

    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    out = tmp_path / "ready"
    assert cli.main(["prepare", str(transcript_file), "-o", str(out)]) == 0
    assert cli.main(["validate", str(out)]) == 0
    assert cli.main(["inspect", str(out)]) == 0
    assert cli.main(["version"]) == 0
