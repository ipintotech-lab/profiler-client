import json
import os
import socket

import pytest
from conftest import CANARIES, canary_forms

from profiler_client import cli
from profiler_client.claude_code import convert_claude_code_transcript
from profiler_client.dataset import EVENTS, PrepareError, prepare, validate
from profiler_client.lint import check_event
from profiler_client.names import Names
from profiler_client.pseudonym import Pseudonymizer, load_or_create_key, new_key


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
