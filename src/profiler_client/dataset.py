"""Read trace files and write / check an ``analysis-ready`` directory:

* ``events.jsonl`` -- a seal line, then contract v2 events, one per line
* ``manifest.json`` -- versions, counts and the SHA-256 of ``events.jsonl``

The seal (``{"profiler_client_seal": {...}}``) records the client, contract
and policy versions, what ``parent`` means (``parent_relation``), the event count, the kept names and the SHA-256 of
the event lines after it, so ``events.jsonl`` can be checked on its own:
the profiler server reads only that file, refuses it without a seal that
matches, and runs ``read_sealed`` -- this same lint -- on every event. The
seal is a checksum, not a signature: anyone can compute one, which is why
the server re-runs the lint instead of trusting it.

The manifest's ``dataset_id`` is random per ``prepare`` and never reused,
so it can't link datasets. Neither file holds the key, file paths or
anything the lint would reject. ``kept_names`` lists every name written in
clear text because of ``--keep-names``; the lint accepts no others.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections import Counter
from pathlib import Path

from pydantic import ValidationError

from . import __version__, policy
from .chat import convert_chat_trajectories, is_chat_trajectory
from .claude_code import Subagents, convert_claude_code_transcript, is_claude_code_transcript
from .codex import convert_codex_session, is_codex_session
from .contract import CONTRACT_VERSION, PARENT_RELATIONS, READABLE_CONTRACTS, TraceEvent
from .events import convert_event_log, is_event_log
from .lint import check_event
from .names import KEEPABLE, Names
from .otel import convert_otel_export, is_otel_export

KEY_ID = re.compile(r"[0-9a-f]{16}")

MAX_LINE_BYTES = 16 * 1024 * 1024
MAX_FILE_BYTES = 1024 * 1024 * 1024

EVENTS = "events.jsonl"
MANIFEST = "manifest.json"
SEAL = "profiler_client_seal"


# What ``parent`` means in each format: see ``contract.PARENT_RELATIONS``.
# A tool call's parent is the LLM call that chose it (Claude Code, Codex,
# chat) or its enclosing span (OTel). An event log's is whatever its author meant.
PARENT_RELATION = {"claude-code": "invokes", "codex": "invokes", "otel": "invokes", "openai-chat": "invokes",
                   "events": "unspecified"}

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


def describe(path: Path) -> dict:
    """A trace file's absolute path, size and SHA-256, to tell later whether it changed."""
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return {"path": str(path.resolve()), "size": path.stat().st_size, "sha256": digest.hexdigest()}


def _read_document(path: Path, jsonl_problem: str) -> list:
    try:
        document = json.loads(path.read_bytes())
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise PrepareError("INPUT_NOT_JSON", f"{path} is neither JSON Lines ({jsonl_problem}) nor one JSON document"
                           ) from None
    return document if isinstance(document, list) else [document]


def _check(events: list, kept: frozenset[str]) -> None:
    for number, event in enumerate(events, start=1):
        if not isinstance(event, dict):
            raise PrepareError("CONTRACT_INVALID", f"event {number} is not a JSON object")
        problems = check_event(event, kept)
        if problems:
            raise PrepareError("PRIVACY_LINT", f"event {number} has fields that may carry clear text: "
                                               + ", ".join(sorted(set(problems))))
        try:
            TraceEvent.model_validate(event)
        except ValidationError as exc:
            fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
            raise PrepareError("CONTRACT_INVALID", f"event {number} breaks the event contract in: "
                                                   f"{', '.join(fields)}") from None


def detect(rows: list) -> str | None:
    return next((name for name, matches, _ in FORMATS if matches(rows)), None)


def subagents_across(paths: list[Path]) -> Subagents:
    """What the Claude Code files among ``paths`` say about their subagents
    (``claude_code.Subagents``): a subagent's file is separate from the
    parent's, and its type and the tool call that started it are in the
    parent's. A file that can't be read is left to ``convert_file`` to refuse."""
    found = Subagents()
    if len(paths) < 2:
        return found  # a single file reads its own
    for path in paths:
        try:
            rows = read_records(path)
        except PrepareError:
            continue
        if detect(rows) == "claude-code":
            found.update(Subagents.read([r for r in rows if isinstance(r, dict)]))
    return found


def convert_file(path: Path, names: Names, subagents: Subagents | None = None) -> tuple[str, object]:
    """Read one trace file and convert it to contract events, in memory.
    ``subagents``: see ``subagents_across``."""
    rows = read_records(path)
    source = detect(rows)
    if source is None:
        raise PrepareError("FORMAT_UNSUPPORTED", f"{path} is not a supported trace format: "
                                                 + ", ".join(name for name, _, _ in FORMATS))
    convert = {name: convert for name, _, convert in FORMATS}[source]
    conversion = convert(rows, names, subagents) if source == "claude-code" else convert(rows, names)
    if not conversion.events:
        raise PrepareError("NO_EVENTS", f"{path} ({source}) has no LLM or tool calls to prepare")
    return source, conversion


def prepare(paths: list[Path], out_dir: Path, names: Names, refs: dict | None = None) -> dict:
    """Convert ``paths`` into a sealed dataset in ``out_dir``. With ``refs``,
    also fill it with where each event came from (see ``trace_refs``)."""
    if out_dir.exists() and any(out_dir.iterdir()):
        raise PrepareError("OUTPUT_NOT_EMPTY", f"{out_dir} already has files; pick an empty or new directory")
    events: list[dict] = []
    sessions = sessions_without_cost = 0
    formats: set[str] = set()
    subagents = subagents_across(paths)
    for file_index, path in enumerate(paths):
        source, conversion = convert_file(path, names, subagents)  # refuses symlinks and non-files first
        if refs is not None:
            refs.setdefault("files", []).append(describe(path))
        for index, (event, where) in enumerate(zip(conversion.events, conversion.sources)):
            if not event.get("event_id"):
                # the source had no id for it: one from its place in the input
                event["event_id"] = names.token("id", ["event", file_index, index])
            if refs is not None and where is not None:
                refs.setdefault("events", {}).setdefault(event["event_id"], [file_index, *where])
        formats.add(source)
        events.extend(conversion.events)
        sessions += conversion.sessions
        sessions_without_cost += conversion.sessions_without_cost
    kept = frozenset(names.kept)
    _check(events, kept)

    relations = {PARENT_RELATION[f] for f in formats}
    relation = relations.pop() if len(relations) == 1 else "mixed"
    payload = sealed_payload(events, kept, names.ps.key_id(), relation)
    manifest = {
        "dataset_id": secrets.token_hex(16),
        "client_version": __version__,
        "contract_version": CONTRACT_VERSION,
        "policy_version": policy.POLICY_VERSION,
        "parent_relation": relation,
        "source_format": "+".join(sorted(formats)),
        "files": len(paths),
        "sessions": sessions,
        "sessions_without_cost": sessions_without_cost,
        "events": len(events),
        "kept_names": sorted(kept),
        "llm_inputs": names.llm_inputs,
        "key_id": names.ps.key_id(),
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / EVENTS).write_bytes(payload)
    (out_dir / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def sealed_payload(events: list[dict], kept: frozenset[str], key_id: str | None = None,
                   parent_relation: str = "unspecified") -> bytes:
    """``events.jsonl``'s bytes: the seal line, then one event per line.
    ``key_id`` (``Pseudonymizer.key_id``) says which datasets share a key."""
    body = "".join(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n" for e in events).encode()
    seal = {
        "client_version": __version__,
        "contract_version": CONTRACT_VERSION,
        "policy_version": policy.POLICY_VERSION,
        "parent_relation": parent_relation,
        "events": len(events),
        "kept_names": sorted(kept),
        "payload_sha256": hashlib.sha256(body).hexdigest(),
    }
    if key_id is not None:
        seal["key_id"] = key_id
    return json.dumps({SEAL: seal}, sort_keys=True).encode() + b"\n" + body


def read_sealed(payload: bytes) -> tuple[dict, list[dict]]:
    """Check ``events.jsonl``'s bytes on their own: a seal first, the
    events after it match its hash and count, and every event passes the
    lint (with the seal's kept names) and the contract. Contract v1
    datasets (no ``event_id``, no ``parent_relation``) are still accepted."""
    first, _, body = payload.partition(b"\n")
    try:
        record = json.loads(first)
    except (json.JSONDecodeError, UnicodeDecodeError):
        record = None
    seal = record.get(SEAL) if isinstance(record, dict) and set(record) == {SEAL} else None
    if not isinstance(seal, dict):
        raise PrepareError("SEAL_MISSING", f"{EVENTS} doesn't start with a profiler-client seal; "
                                           "prepare it with profiler-client")
    if seal.get("contract_version") not in READABLE_CONTRACTS:
        raise PrepareError("SEAL_UNSUPPORTED", "the dataset's contract version isn't one this client reads ("
                                               + ", ".join(f"v{v}" for v in sorted(READABLE_CONTRACTS))
                                               + "); prepare it with a matching profiler-client")
    if seal["contract_version"] != "1" and seal.get("parent_relation") not in PARENT_RELATIONS:
        raise PrepareError("SEAL_INVALID", "parent_relation must be one of "
                                           + ", ".join(sorted(PARENT_RELATIONS)))
    if hashlib.sha256(body).hexdigest() != seal.get("payload_sha256"):
        raise PrepareError("PAYLOAD_CHANGED", "the events no longer match the seal; run prepare again")
    try:
        events = [json.loads(line) for line in body.decode().splitlines() if line.strip()]
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise PrepareError("PAYLOAD_CHANGED", "an event line is not JSON") from None
    if len(events) != seal.get("events"):
        raise PrepareError("PAYLOAD_CHANGED", "the event count doesn't match the seal")
    kept = seal.get("kept_names")
    if not isinstance(kept, list) or not all(isinstance(k, str) and KEEPABLE.fullmatch(k) for k in kept):
        raise PrepareError("SEAL_INVALID", "kept_names must be a list of single-word names")
    if "key_id" in seal and not (isinstance(seal["key_id"], str) and KEY_ID.fullmatch(seal["key_id"])):
        raise PrepareError("SEAL_INVALID", "key_id must be 16 lowercase hex characters")
    _check(events, frozenset(kept))
    return seal, events


def validate(out_dir: Path) -> tuple[dict, list[dict]]:
    """Re-check a prepared directory: the payload still matches the
    manifest's hash, the manifest agrees with the seal, and every event
    passes the lint and the contract."""
    try:
        manifest = json.loads((out_dir / MANIFEST).read_text(encoding="utf-8"))
        payload = (out_dir / EVENTS).read_bytes()
    except (OSError, json.JSONDecodeError) as exc:
        raise PrepareError("DATASET_UNREADABLE", f"{out_dir}: {type(exc).__name__}") from None
    if hashlib.sha256(payload).hexdigest() != manifest.get("payload_sha256"):
        raise PrepareError("PAYLOAD_CHANGED", f"{EVENTS} no longer matches the manifest; run prepare again")
    seal, events = read_sealed(payload)
    if manifest.get("events") != seal["events"] or manifest.get("kept_names") != seal["kept_names"]:
        raise PrepareError("MANIFEST_INVALID", "the manifest's events or kept_names don't match the seal")
    return manifest, events


def summary(events: list[dict]) -> dict:
    return {
        "event_types": dict(Counter(e["event_type"] for e in events)),
        "top_nodes": Counter(e["node"] for e in events).most_common(15),
        "cost": round(sum(e.get("cost", 0.0) for e in events), 6),
    }
