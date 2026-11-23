"""Savings held for review because the trace didn't record what the model chose (the server's
``held_unrecorded``, agents_profiler #126): the report shows them as a line under the headline, beside
"One answer away", so a run whose model outputs weren't recorded never reads as a bare 0%.

The fixtures are real results of the server's overview job (agents_profiler 3b6a515) over the loan flow
(``generate_caseflows.py --format otel --tool-call-ids --specialist-llm --days 2 --scale 0.5``), prepared
with --keep-names: ``full.json.gz`` as generated, ``stripped.json.gz`` with the message content removed
from every span (what an exporter that records metadata only sends).
"""
import copy
import gzip
import json
from pathlib import Path

import pytest
from test_savings import _render

from profiler_client.report import view

DATA = Path(__file__).parent / "data" / "held-unrecorded"
HELD = "is held for review because model outputs weren't recorded; send traces that include them to confirm."


def _load(name: str) -> dict:
    with gzip.open(DATA / f"{name}.json.gz", "rt", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def results():
    return {name: _load(name) for name in ("full", "stripped")}


def _all_text(slots: dict) -> str:
    return " ".join(s["text"] for s in slots.values())


def test_a_stripped_overview_shows_the_held_line_under_the_headline(results):
    data = results["stripped"]
    slots = _render(data)["slots"]
    held = slots["svHeld"]
    assert not held["hidden"]
    assert "Held for review" in held["text"]
    text = held["text"].replace("&#39;", "'")
    assert data["overview"]["held_unrecorded"]["text"] in text and text.endswith(HELD)


def test_a_full_data_overview_has_no_held_line(results):
    slots = _render(results["full"])["slots"]
    assert results["full"]["overview"]["held_unrecorded"] is None
    assert slots["svHeld"]["hidden"] and not slots["svHeld"]["text"]


def test_a_workflow_report_shows_its_own_held_line(results):
    for name, shown in (("stripped", True), ("full", False)):
        report = next(iter(results[name]["reports"].values()))
        slots = _render(report)["slots"]
        assert slots["held"]["hidden"] is not shown, name
        if shown:
            assert "Held for review" in slots["held"]["text"]
            assert "Another $" in slots["held"]["text"]


def test_a_held_finding_gives_the_servers_reason_in_all_findings(results):
    report = copy.deepcopy(next(iter(results["stripped"]["reports"].values())))
    # the table shows the counted rows first, ten at a time: keep only the held ones so they are shown
    rows = [f for f in report["summary"]["findings"] if f["held_out"] == "unrecorded" and f["covered_by"] is None]
    report["summary"]["findings"] = rows
    types = {f["type"] for f in rows}
    assert types == {"CONDITIONAL_LLM_BYPASS", "EXTRACT_DETERMINISTIC_SUBSTEP"}
    text = _all_text(_render(report)["slots"]).replace("&#39;", "'")
    assert "What the model chose here wasn't recorded; send traces that include its output to confirm." in text
    assert ("Some steps between these weren't recorded in full; send traces that include model inputs and "
            "outputs to confirm.") in text


def test_the_page_profiler_client_open_writes_keeps_the_held_line(results):
    """`open` draws view(report), which keeps only what report.js draws: the held totals must be in it."""
    page = view(results["stripped"])
    assert page["overview"]["held_unrecorded"] == results["stripped"]["overview"]["held_unrecorded"]
    assert all(r["summary"]["held_unrecorded"] for r in page["reports"].values())
    assert not _render(page)["slots"]["svHeld"]["hidden"]
    full = view(results["full"])
    assert full["overview"]["held_unrecorded"] is None
    assert all(r["summary"]["held_unrecorded"] is None for r in full["reports"].values())
