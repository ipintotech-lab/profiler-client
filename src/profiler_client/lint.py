"""Privacy lint: every string a prepared event holds must be a pseudonym or
built only from ``policy.py`` words. It allows what it recognizes instead
of searching for secrets, so an encoding it has never seen (base64, URL
escapes, look-alike Unicode, a value split across fields) fails too.

Names kept with ``--keep-names`` are allowed only when passed in ``kept``
(the manifest's ``kept_names``), so a name nobody reviewed fails too.

Violations name the field, never the value.
"""
from __future__ import annotations

import re
from typing import Any

from . import policy
from .pseudonym import TOKEN

_TOKEN_RE = re.compile(rf"^{TOKEN.pattern}$")
_SEPARATORS = re.compile(r"[\s:+]+")
_RESULT = re.compile(rf"^(success|error):{TOKEN.pattern}$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_MODEL = re.compile(rf"^({'|'.join(re.escape(p) for p in policy.MODEL_PREFIXES)})[a-z0-9.-]*$")

_NUMBERS = ("latency_ms", "cost")
_FLAGS = ("input_complete", "has_side_effects", "idempotent", "cache_eligible")


def _is_phrase(text: str, kept: frozenset[str]) -> bool:
    """A node, trigger or selection: policy words, kept names and pseudonyms only."""
    text = TOKEN.sub(" ", text).replace("(error)", " ")
    return all(word in policy.VOCABULARY or word in kept for word in _SEPARATORS.split(text) if word)


def _is_name(value: Any, allowed: frozenset[str], kept: frozenset[str]) -> bool:
    return isinstance(value, str) and (value in allowed or value in kept or bool(_TOKEN_RE.match(value)))


def _is_workflow(value: Any, kept: frozenset[str]) -> bool:
    if not isinstance(value, str):
        return False
    source, sep, rest = value.partition("/")
    if sep and source in policy.SOURCES and _is_name(rest, frozenset(), kept):
        return True
    return _is_name(value, frozenset(), kept)


def _check_string(path: str, value: Any, ok: bool, problems: list[str]) -> None:
    if not isinstance(value, str) or not ok:
        problems.append(path)


def check_event(event: dict, kept: frozenset[str] = frozenset()) -> list[str]:
    """Paths of the fields that may carry clear text; empty when clean."""
    problems: list[str] = []
    for key, value in event.items():
        if value is None:
            continue
        if key in ("trace_id", "run_id", "event_id", "parent"):
            _check_string(key, value, isinstance(value, str) and bool(_TOKEN_RE.match(value)), problems)
        elif key == "workflow":
            _check_string(key, value, _is_workflow(value, kept), problems)
        elif key == "agent":
            _check_string(key, value, _is_name(value, policy.AGENTS, kept), problems)
        elif key == "event_type":
            _check_string(key, value, _is_name(value, policy.EVENT_TYPES, kept), problems)
        elif key in ("node", "trigger"):
            _check_string(key, value, isinstance(value, str) and _is_phrase(value, kept), problems)
        elif key == "model":
            ok = isinstance(value, str) and bool(_MODEL.match(value) or _TOKEN_RE.match(value))
            _check_string(key, value, ok, problems)
        elif key == "workflow_version":
            _check_string(key, value, isinstance(value, str) and bool(_VERSION.match(value)), problems)
        elif key == "result":
            ok = value == "no_result" or value in policy.STOP_REASONS or bool(_RESULT.match(str(value)))
            _check_string(key, value, isinstance(value, str) and ok, problems)
        elif key == "input_state":
            if not isinstance(value, dict):
                problems.append(key)
                continue
            for k, v in value.items():
                if k == "input":
                    _check_string(f"input_state.{k}", v, isinstance(v, str) and bool(_TOKEN_RE.match(v)), problems)
                elif k == "trigger":
                    _check_string(f"input_state.{k}", v, isinstance(v, str) and _is_phrase(v, kept), problems)
                elif (_TOKEN_RE.match(k) or k in kept) and isinstance(v, str) and _TOKEN_RE.match(v):
                    pass  # an event log's field: a pseudonym or kept name, its value a pseudonym
                else:
                    problems.append(f"input_state.{k}")
        elif key == "decision":
            if not isinstance(value, dict) or set(value) != {"type", "selected"}:
                problems.append(key)
                continue
            ok = _is_name(value["type"], frozenset({"next_action"}), kept)
            _check_string("decision.type", value["type"], ok, problems)
            sel = value["selected"]
            _check_string("decision.selected", sel, isinstance(sel, str) and _is_phrase(sel, kept), problems)
        elif key == "tool":
            name = value.get("name") if isinstance(value, dict) else None
            ok = isinstance(value, dict) and set(value) == {"name"} and _is_name(name, policy.TOOLS, kept)
            _check_string("tool.name", name, ok, problems)
        elif key == "tokens":
            ok = isinstance(value, dict) and set(value) <= {"input", "output"} and all(
                isinstance(v, int) and not isinstance(v, bool) for v in value.values())
            if not ok:
                problems.append(key)
        elif key == "cache_ttl_seconds":
            if not isinstance(value, int) or isinstance(value, bool):
                problems.append(key)
        elif key in _NUMBERS:
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                problems.append(key)
        elif key in _FLAGS:
            if not isinstance(value, bool):
                problems.append(key)
        else:
            # user_input, invalidation_keys and anything new: never written
            problems.append(key)
    return problems
