"""``show-event``: an event id from a report leads back, on this machine
only, to the record it came from in the original trace file."""
import json
import os

import pytest
from test_formats import FORMATS

from profiler_client import cli
from profiler_client.dataset import EVENTS, read_sealed
from profiler_client.names import Names
from profiler_client.pseudonym import Pseudonymizer, load_or_create_key

SOURCES = {**FORMATS, "claude-code": None}


def _prepare(tmp_path, monkeypatch, source, transcript_file, *extra):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    trace = transcript_file if source == "claude-code" else FORMATS[source](tmp_path)
    key = tmp_path / "key"
    out = tmp_path / "ready"
    assert cli.main(["prepare", str(trace), "-o", str(out), "--key-file", str(key), *extra]) == 0
    seal, events = read_sealed((out / EVENTS).read_bytes())
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"payload_sha256": seal["payload_sha256"]}))
    return trace, report, events, Names(Pseudonymizer(load_or_create_key(key))), seal["payload_sha256"]


def _show(report, event_id, capsys):
    capsys.readouterr()
    code = cli.main(["show-event", str(report), event_id])
    out, err = capsys.readouterr()
    return code, out, err


@pytest.mark.parametrize("source", SOURCES)
def test_every_event_resolves_to_its_original_record(tmp_path, monkeypatch, capsys, source, transcript_file):
    _, report, events, names, _ = _prepare(tmp_path, monkeypatch, source, transcript_file)
    for event in events:
        code, out, err = _show(report, event["event_id"], capsys)
        assert code == 0, err
        shown = json.loads(out)
        assert isinstance(shown, dict)
        # the record the event's id was made from
        if source == "claude-code":
            if event["event_type"] == "tool_call":
                assert shown["type"] == "tool_use" and event["event_id"] == names.token("id", ["tool", shown["id"]])
            else:
                assert event["event_id"] == names.token("id", shown["message"]["id"])
        elif source == "otel":
            assert event["event_id"] == names.token("id", shown["spanId"])
        elif source == "openai-chat":
            assert shown["role"] == "assistant"
        elif source == "codex":
            assert shown["type"] == "response_item"
            if event["event_type"] == "tool_call":
                assert event["event_id"] == names.token("id", ["tool", "sess-acme-secret-project-1",
                                                               shown["payload"]["call_id"]])
        else:
            assert shown["type"] == {"decision": "llm"}.get(event["event_type"], event["event_type"])


def test_a_changed_or_missing_file_is_said_plainly_and_nothing_is_shown(tmp_path, monkeypatch, capsys,
                                                                        transcript_file):
    trace, report, events, _, _ = _prepare(tmp_path, monkeypatch, "claude-code", transcript_file)
    event_id = events[0]["event_id"]
    trace.write_text(trace.read_text().replace("Acme Corp", "Acme Inc"))
    code, out, err = _show(report, event_id, capsys)
    assert code == 1 and out == "" and "has changed" in err
    trace.unlink()
    code, out, err = _show(report, event_id, capsys)
    assert code == 1 and out == "" and "no longer there" in err
    assert str(trace) not in err and trace.name not in err and "Acme" not in err


def test_an_unknown_event_or_dataset_is_refused(tmp_path, monkeypatch, capsys, transcript_file):
    _, report, _, _, _ = _prepare(tmp_path, monkeypatch, "claude-code", transcript_file)
    code, out, err = _show(report, "h:" + "0" * 64, capsys)
    assert code == 1 and out == "" and "isn't in" in err
    assert _show(report, "not-an-id", capsys)[0] == 1
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"payload_sha256": "f" * 64}))
    code, out, err = _show(other, "h:" + "0" * 64, capsys)
    assert code == 1 and "no event references" in err


def test_refs_are_private_skipped_without_a_name_list_and_forgotten(tmp_path, monkeypatch, capsys, transcript_file):
    _, report, events, _, digest = _prepare(tmp_path, monkeypatch, "claude-code", transcript_file)
    refs = tmp_path / "data" / "profiler-client" / "refs" / f"{digest}.json"
    assert os.stat(refs).st_mode & 0o777 == 0o600
    # the trace's contents never go into the refs, only where to find them
    assert "Acme" not in refs.read_text()
    assert cli.main(["forget", str(report)]) == 0
    assert not refs.exists()
    assert _show(report, events[0]["event_id"], capsys)[0] == 1

    (tmp_path / "ready").rename(tmp_path / "ready-1")
    _prepare(tmp_path, monkeypatch, "claude-code", transcript_file, "--no-name-list")
    assert list(refs.parent.iterdir()) == []


def test_forget_all_removes_every_refs_file(tmp_path, monkeypatch, transcript_file):
    _, _, _, _, digest = _prepare(tmp_path, monkeypatch, "claude-code", transcript_file)
    refs_dir = tmp_path / "data" / "profiler-client" / "refs"
    assert (refs_dir / f"{digest}.json").exists()
    assert cli.main(["forget", "--all"]) == 0
    assert list(refs_dir.glob("*.json")) == []
