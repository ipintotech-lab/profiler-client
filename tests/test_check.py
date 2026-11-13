import json
import socket

import pytest
from conftest import CANARIES

from profiler_client.cli import main
from profiler_client.readiness import GOOD_RUNS, MIN_BYPASS_OBSERVATIONS, SMALL_DATASET_EVENTS


def _log(tmp_path, runs=25, cost=True, tool_input=True, name="events.json"):
    """An event log of ``runs`` runs of one Acme workflow: an LLM call that
    picks a tool, then the tool."""
    events = []
    for i in range(runs):
        common = {"traceId": f"acme-trace-{i}", "runId": f"acme-run-{i}", "workflow_name": "acme-secret-project",
                  "agent": "acme-agent"}
        llm = {**common, "type": "llm", "name": "plan", "model": "acme-finetune-v3", "duration_ms": 900,
               "prompt_tokens": 120, "completion_tokens": 10, "decision": {"type": "next_action", "selected": "corp_cli"}}
        if cost:
            llm["cost_usd"] = 0.002
        tool = {**common, "type": "tool_call", "name": "corp_cli", "tool_name": "corp_cli", "duration_ms": 30,
                "output": "customer list for Acme Corp"}
        if tool_input:
            tool["input_state"] = {"token": "ACME_TOKEN_9f8e7d"}
        events += [llm, tool]
    path = tmp_path / name
    path.write_text(json.dumps({"events": events}), encoding="utf-8")
    return path


def _check(capsys, *args):
    capsys.readouterr()
    code = main(["check", *map(str, args), "--json"])
    return code, json.loads(capsys.readouterr().out)


def _found(out):
    return {f["key"]: f["status"] for f in out["findings_possible"]}


def test_a_complete_log_can_make_every_finding(tmp_path, capsys):
    code, out = _check(capsys, _log(tmp_path))
    assert code == 0
    assert out["readable"] == {"files": 1, "read": 1, "formats": {"events": 1}, "skipped": []}
    assert out["dataset"]["grade"]["grade"] == "A"
    found = _found(out)
    assert found["reuse_result"] == found["unstable_result"] == found["dollar_figures"] == "yes"
    assert found["rule_for_llm"] == "yes" and found["script_sequence"] == "yes"


def test_without_tool_arguments_reuse_and_unstable_are_not_possible(tmp_path, capsys):
    _, out = _check(capsys, _log(tmp_path, tool_input=False))
    assert _found(out)["reuse_result"] == _found(out)["unstable_result"] == "no"
    assert any("tool arguments" in line for line in out["remediation"])


def test_tokens_without_cost_need_prices(tmp_path, capsys):
    path = _log(tmp_path, cost=False)
    _, out = _check(capsys, path)
    assert _found(out)["dollar_figures"] == "limited" and _found(out)["rule_for_llm"] == "no"
    assert any("--prices" in line for line in out["remediation"])
    _, priced = _check(capsys, path, "--prices", "1.25,10")
    assert _found(priced)["dollar_figures"] == "yes" and _found(priced)["rule_for_llm"] == "yes"
    assert not any("--prices" in line for line in priced["remediation"])


def test_few_runs_and_a_small_dataset_are_named(tmp_path, capsys):
    _, out = _check(capsys, _log(tmp_path, runs=MIN_BYPASS_OBSERVATIONS - 1))
    assert _found(out)["script_sequence"] == "limited" and _found(out)["rule_for_llm"] == "no"
    assert out["dataset"]["small"] and out["dataset"]["events"] < SMALL_DATASET_EVENTS
    assert any(f"fewer than {GOOD_RUNS} runs" in line for line in out["remediation"])
    assert any(f"{SMALL_DATASET_EVENTS:,}" in line for line in out["remediation"])


def test_min_grade_fails_a_thin_workflow(tmp_path, capsys):
    path = _log(tmp_path, runs=2, cost=False, tool_input=False)
    _, out = _check(capsys, path)
    assert out["workflows"][0]["grade"]["grade"] in "CD"
    assert main(["check", str(path), "--min-grade", "B"]) == 1
    assert main(["check", str(_log(tmp_path, name="good.json")), "--min-grade", "B"]) == 0


def test_unreadable_files_exit_2_with_a_reason_each(tmp_path, capsys):
    bad = tmp_path / "notes.json"
    bad.write_text(json.dumps({"hello": "world"}), encoding="utf-8")
    code, out = _check(capsys, bad, tmp_path)
    assert code == 2
    assert [s["code"] for s in out["readable"]["skipped"]] == ["FORMAT_UNSUPPORTED", "INPUT_NOT_FILE"]
    assert all(s["file"] is None for s in out["readable"]["skipped"])  # names only with --names


def test_output_has_no_trace_content_and_names_only_when_asked(tmp_path, capsys):
    path = _log(tmp_path)
    for flags in ([], ["--json"]):
        capsys.readouterr()
        main(["check", str(path), *flags])
        out = capsys.readouterr().out
        for canary in [*CANARIES, "acme-run-1", "acme-trace-1", "acme-agent", "plan"]:
            assert canary not in out, canary
    capsys.readouterr()
    main(["check", str(path), "--json", "--names"])
    assert json.loads(capsys.readouterr().out)["workflows"][0]["name"] == "acme-secret-project"


def test_check_writes_nothing_and_opens_no_connection(tmp_path, capsys, monkeypatch):
    path = _log(tmp_path)

    def refuse(*args, **kwargs):
        raise AssertionError("check opened a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    before = sorted(tmp_path.rglob("*"))
    assert main(["check", str(path)]) == 0
    assert sorted(tmp_path.rglob("*")) == before


@pytest.mark.parametrize("prices", ["1,2,3", "x,1", "-1,2"])
def test_bad_prices_are_refused(tmp_path, prices):
    assert main(["check", str(_log(tmp_path)), f"--prices={prices}"]) == 1
