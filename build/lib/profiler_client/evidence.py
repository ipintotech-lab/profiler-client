"""A bounded, deterministic sample of the events behind a finding.

Each entry is ``{"run_id", "event_id"}`` -- enough for profiler-client to
find the original record on the customer's machine, nothing of its
content. Only events with an ``event_id`` (contract v2) can be pointed
at, so a v1 dataset's samples are empty.
"""
from __future__ import annotations

from collections.abc import Iterable

from .contract import TraceEvent

EVIDENCE_SAMPLE = 5


def evidence_sample(events: Iterable[TraceEvent]) -> list[dict]:
    """The ``EVIDENCE_SAMPLE`` lowest event ids among ``events``."""
    ided = sorted({e.event_id: e for e in events if e.event_id}.items())
    return [{"run_id": e.run_id, "event_id": eid} for eid, e in ided[:EVIDENCE_SAMPLE]]
