"""Keyed pseudonyms. A value becomes ``h:<64 hex>`` = HMAC-SHA256 over a
domain and the value's canonical JSON, so equal values stay equal (which
is all the profiler needs) while nobody without the key can test a guess.

Domains keep a value from matching itself across roles: the same string
as a file name and as tool output gives different pseudonyms.

The key never leaves the machine and is never written next to the
prepared data. By default every ``prepare`` uses a fresh key, so two
datasets can't be joined; a persistent key file is opt-in, for comparing
runs over time, and losing it breaks that comparison.

``key_id`` names the key without revealing it, so the server can tell
whether two datasets share a key -- and can be combined into one report --
which their equal pseudonyms would already show.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from pathlib import Path
from typing import Any

DOMAINS = ("id", "name", "input", "output", "model")

TOKEN = re.compile(r"h:[0-9a-f]{64}")

KEY_BYTES = 32


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()


class Pseudonymizer:
    def __init__(self, key: bytes) -> None:
        if len(key) < KEY_BYTES:
            raise ValueError(f"the key must be at least {KEY_BYTES} bytes")
        self._key = key

    def token(self, domain: str, value: Any) -> str:
        if domain not in DOMAINS:
            raise ValueError(f"unknown pseudonym domain {domain!r}")
        mac = hmac.new(self._key, domain.encode() + b"\0" + canonical(value), hashlib.sha256)
        return "h:" + mac.hexdigest()

    def key_id(self) -> str:
        """16 hex characters naming the key; equal only for the same key."""
        return hmac.new(self._key, b"key-id", hashlib.sha256).hexdigest()[:16]


def new_key() -> bytes:
    return secrets.token_bytes(KEY_BYTES)


def load_or_create_key(path: Path) -> bytes:
    """Read a persistent key, creating it (mode 0600) if it doesn't exist."""
    if path.exists():
        if path.stat().st_mode & 0o077:
            raise ValueError(f"{path} is readable by other users; run: chmod 600 {path}")
        key = bytes.fromhex(path.read_text(encoding="ascii").strip())
        if len(key) < KEY_BYTES:
            raise ValueError(f"{path} holds a key shorter than {KEY_BYTES} bytes")
        return key
    path.parent.mkdir(parents=True, exist_ok=True)
    key = new_key()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(key.hex() + "\n")
    return key
