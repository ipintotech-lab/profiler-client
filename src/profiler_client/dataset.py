"""Read trace files and write / check an ``analysis-ready`` directory:

* ``events.jsonl`` -- contract v1 events, one per line
* ``manifest.json`` -- versions, counts and the SHA-256 of ``events.jsonl``

The manifest's ``dataset_id`` is random per ``prepare`` and never reused,
so it can't link datasets. Neither file holds the key, file paths or
anything the lint would reject. ``kept_names`` lists every name written in
clear text because of ``--keep-names``; the lint accepts no others.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from collections import Counter
from pathlib import Path

from pydantic import ValidationError

from . import __version__, policy
from .chat import convert_chat_trajectories, is_chat_trajectory
from .claude_code import convert_claude_code_transcript, is_claude_code_transcript
from .codex import convert_codex_session, is_codex_session
from .contract import CONTRACT_VERSION, TraceEvent
from .events import convert_event_log, is_event_log
from .lint import check_event
from .names import KEEPABLE, Names
from .otel import convert_otel_export, is_otel_export

MAX_LINE_BYTES = 16 * 1024 * 1024
MAX_FILE_BYTES = 1024 * 1024 * 1024

EVENTS = "events.jsonl"
MANIFEST = "manifest.json"


# Checked in order: the generic event log last, since it accepts the most.
FORMATS = (
    ("claude-code", is_claude_code_transcript, convert_claude_code_transcript),
    ("codex", is_codex_session, convert_codex_session),
    ("otel", is_otel_export, convert_otel_export),
    ("openai-chat", lambda rows: bool(rows) and all(is_chat_trajectory(r) for r in rows), convert_chat_trajectories),
    ("events", is_event_log, convert_event_log),
)


class PrepareError(Exception):
    """A stable error code plus a message that never quotes file content."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def read_records(path: Path) -> list:
    """Parse one trace file as JSON Lines, line by line with a per-line cap,
    or else as one JSON document (a list is its records). Symlinks and
    directories are refused: the user names every file that is read."""
    if path.is_symlink():
        raise PrepareError("INPUT_SYMLINK", f"{path} is a symlink; pass the file it points to")
    if not path.is_file():
        raise PrepareError("INPUT_NOT_FILE", f"{path} is not a regular file")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise PrepareError("INPUT_TOO_LARGE", f"{path} is larger than {MAX_FILE_BYTES} bytes")
    rows = []
    with path.open("rb") as f:
        for number, line in enumerate(iter(lambda: f.readline(MAX_LINE_BYTES + 1), b""), start=1):
            if len(line) > MAX_LINE_BYTES:
                return _read_document(path, f"line {number} is longer than {MAX_LINE_BYTES} bytes")
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return _read_document(path, f"line {number} is not JSON")
    return rows


def _read_document(path: Path, jsonl_problem: str) -> list:
    try:
        document = json.loads(path.read_bytes())
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise PrepareError("INPUT_NOT_JSON", f"{path} is neither JSON Lines ({jsonl_problem}) nor one JSON document"
                           ) from None
    return document if isinstance(document, list) else [document]


def _check(events: list[dict], kept: frozenset[str]) -> None:
    for number, event in enumerate(events, start=1):
        problems = check_event(event, kept)
        if problems:
            raise PrepareError("PRIVACY_LINT", f"event {number} has fields that may carry clear text: "
                                               + ", ".join(sorted(set(problems))))
        try:
            TraceEvent.model_validate(event)
        except ValidationError as exc:
            fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
            raise PrepareError("CONTRACT_INVALID", f"event {number} breaks contract v1 in: {', '.join(fields)}") from None


def detect(rows: list) -> str | None:
    return next((name for name, matches, _ in FORMATS if matches(rows)), None)


def prepare(paths: list[Path], out_dir: Path, names: Names) -> dict:
    if out_dir.exists() and any(out_dir.iterdir()):
        raise PrepareError("OUTPUT_NOT_EMPTY", f"{out_dir} already has files; pick an empty or new directory")
    events: list[dict] = []
    sessions = sessions_without_cost = 0
    formats: set[str] = set()
    converters = {name: convert for name, _, convert in FORMATS}
    for path in paths:
        rows = read_records(path)
        source = detect(rows)
        if source is None:
            raise PrepareError("FORMAT_UNSUPPORTED", f"{path} is not a supported trace format: "
                                                     + ", ".join(name for name, _, _ in FORMATS))
        conversion = converters[source](rows, names)
        if not conversion.events:
            raise PrepareError("NO_EVENTS", f"{path} ({source}) has no LLM or tool calls to prepare")
        formats.add(source)
        events.extend(conversion.events)
        sessions += conversion.sessions
        sessions_without_cost += conversion.sessions_without_cost
    kept = frozenset(names.kept)
    _check(events, kept)

    payload = "".join(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n" for e in events).encode()
    manifest = {
        "dataset_id": secrets.token_hex(16),
        "client_version": __version__,
        "contract_version": CONTRACT_VERSION,
        "policy_version": policy.POLICY_VERSION,
        "source_format": "+".join(sorted(formats)),
        "files": len(paths),
        "sessions": sessions,
        "sessions_without_cost": sessions_without_cost,
        "events": len(events),
        "kept_names": sorted(kept),
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / EVENTS).write_bytes(payload)
    (out_dir / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def validate(out_dir: Path) -> tuple[dict, list[dict]]:
    """Re-check a prepared directory: the payload still matches the
    manifest's hash, and every event passes the lint and the contract."""
    try:
        manifest = json.loads((out_dir / MANIFEST).read_text(encoding="utf-8"))
        payload = (out_dir / EVENTS).read_bytes()
    except (OSError, json.JSONDecodeError) as exc:
        raise PrepareError("DATASET_UNREADABLE", f"{out_dir}: {type(exc).__name__}") from None
    if hashlib.sha256(payload).hexdigest() != manifest.get("payload_sha256"):
        raise PrepareError("PAYLOAD_CHANGED", f"{EVENTS} no longer matches the manifest; run prepare again")
    events = [json.loads(line) for line in payload.decode().splitlines() if line.strip()]
    if len(events) != manifest.get("events"):
        raise PrepareError("PAYLOAD_CHANGED", "the event count doesn't match the manifest")
    kept = manifest.get("kept_names", [])
    if not isinstance(kept, list) or not all(isinstance(k, str) and KEEPABLE.fullmatch(k) for k in kept):
        raise PrepareError("MANIFEST_INVALID", "kept_names must be a list of single-word names")
    _check(events, frozenset(kept))
    return manifest, events


def summary(events: list[dict]) -> dict:
    return {
        "event_types": dict(Counter(e["event_type"] for e in events)),
        "top_nodes": Counter(e["node"] for e in events).most_common(15),
        "cost": round(sum(e.get("cost", 0.0) for e in events), 6),
    }
