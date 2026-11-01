"""Read trace files and write / check an ``analysis-ready`` directory:

* ``events.jsonl`` -- contract v1 events, one per line
* ``manifest.json`` -- versions, counts and the SHA-256 of ``events.jsonl``

The manifest's ``dataset_id`` is random per ``prepare`` and never reused,
so it can't link datasets. Neither file holds the key, file paths or
anything the lint would reject.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from collections import Counter
from pathlib import Path

from pydantic import ValidationError

from . import __version__, policy
from .claude_code import convert_claude_code_transcript, is_claude_code_transcript
from .contract import CONTRACT_VERSION, TraceEvent
from .lint import check_event
from .pseudonym import Pseudonymizer

MAX_LINE_BYTES = 16 * 1024 * 1024
MAX_FILE_BYTES = 1024 * 1024 * 1024

EVENTS = "events.jsonl"
MANIFEST = "manifest.json"


class PrepareError(Exception):
    """A stable error code plus a message that never quotes file content."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def read_jsonl(path: Path) -> list[dict]:
    """Parse one transcript, line by line with a per-line cap. Symlinks and
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
                raise PrepareError("LINE_TOO_LARGE", f"{path} line {number} is longer than {MAX_LINE_BYTES} bytes")
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise PrepareError("INPUT_NOT_JSONL", f"{path} line {number} is not JSON") from None
    return rows


def _check(events: list[dict]) -> None:
    for number, event in enumerate(events, start=1):
        problems = check_event(event)
        if problems:
            raise PrepareError("PRIVACY_LINT", f"event {number} has fields that may carry clear text: "
                                               + ", ".join(sorted(set(problems))))
        try:
            TraceEvent.model_validate(event)
        except ValidationError as exc:
            fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
            raise PrepareError("CONTRACT_INVALID", f"event {number} breaks contract v1 in: {', '.join(fields)}") from None


def prepare(paths: list[Path], out_dir: Path, ps: Pseudonymizer) -> dict:
    if out_dir.exists() and any(out_dir.iterdir()):
        raise PrepareError("OUTPUT_NOT_EMPTY", f"{out_dir} already has files; pick an empty or new directory")
    events: list[dict] = []
    sessions = sessions_without_cost = 0
    for path in paths:
        rows = read_jsonl(path)
        if not is_claude_code_transcript(rows):
            raise PrepareError("FORMAT_UNSUPPORTED", f"{path} is not a Claude Code transcript")
        conversion = convert_claude_code_transcript(rows, ps)
        events.extend(conversion.events)
        sessions += conversion.sessions
        sessions_without_cost += conversion.sessions_without_cost
    _check(events)

    payload = "".join(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n" for e in events).encode()
    manifest = {
        "dataset_id": secrets.token_hex(16),
        "client_version": __version__,
        "contract_version": CONTRACT_VERSION,
        "policy_version": policy.POLICY_VERSION,
        "source_format": "claude-code",
        "files": len(paths),
        "sessions": sessions,
        "sessions_without_cost": sessions_without_cost,
        "events": len(events),
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
    _check(events)
    return manifest, events


def summary(events: list[dict]) -> dict:
    return {
        "event_types": dict(Counter(e["event_type"] for e in events)),
        "top_nodes": Counter(e["node"] for e in events).most_common(15),
        "cost": round(sum(e.get("cost", 0.0) for e in events), 6),
    }
