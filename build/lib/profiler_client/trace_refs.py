"""Where each event of a dataset came from, kept on this machine.

``prepare`` writes ``$XDG_DATA_HOME/profiler-client/refs/<payload_sha256>.json``
next to the name list, under the same rules: mode 0600, outside the dataset
directory, never uploaded, skipped with ``--no-name-list`` and deleted by
``forget``. It holds each trace file's path, size and SHA-256, and for each
event id the file, the record and the place inside the record it came from.

``show-event`` prints an event's original record from the original file,
after checking the file is still there and unchanged; if it isn't, it says
so and prints nothing from the file.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .dataset import describe, read_records
from .report import name_list_dir, write_private

REFS_VERSION = 1


def refs_dir() -> Path:
    return name_list_dir().parent / "refs"


def write_refs(payload_sha256: str, refs: dict, directory: Path | None = None) -> Path:
    return write_private(directory or refs_dir(), payload_sha256, {
        "version": REFS_VERSION, "payload_sha256": payload_sha256,
        "files": refs.get("files", []), "events": refs.get("events", {}),
    })


def read_refs(payload_sha256: str, directory: Path | None = None) -> dict | None:
    """The references for one dataset, or None if this machine has none."""
    if not re.fullmatch(r"[a-f0-9]{64}", payload_sha256):
        return None
    try:
        data = json.loads(((directory or refs_dir()) / f"{payload_sha256}.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (isinstance(data, dict) and data.get("payload_sha256") == payload_sha256
            and isinstance(data.get("files"), list) and isinstance(data.get("events"), dict)):
        return data
    return None


def original_record(refs: dict, event_id: str) -> Any:
    """The record ``event_id`` came from, read again from its file. Errors
    say which file by its number only, never its name or content."""
    file_index, record, *path = refs["events"][event_id]
    known = refs["files"][file_index]
    which = f"file {file_index + 1} of {len(refs['files'])} of this dataset"
    source = Path(known["path"])
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"the original trace file ({which}) is no longer there, so nothing is shown")
    if describe(source) != known:
        raise ValueError(f"the original trace file ({which}) has changed since prepare, so nothing is shown")
    value = read_records(source)[record]
    for step in path:
        value = value[step]
    return value
