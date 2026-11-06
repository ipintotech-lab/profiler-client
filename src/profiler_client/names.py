"""Names a converter writes: pseudonyms by default, and with ``--keep-names``
the step names the user chose -- workflow, agent, node and tool names --
in clear text.

Every name that becomes a pseudonym is also remembered in ``seen``
(pseudonym -> name), so ``prepare`` can keep a name list on this machine
and ``open`` can put the names back into a report. Inputs, outputs and
ids are never remembered.

Only name-like strings are kept: one word of ``[A-Za-z0-9_./-]``, at most
80 characters. A sentence, a command or anything with spaces stays a
pseudonym even with ``--keep-names``. Every kept name is recorded in the
manifest, so ``inspect`` shows exactly which ones would leave the machine,
and the lint accepts only those.

Contents -- inputs, outputs, ids, commands, script names -- are never kept.

An event log's input is written one pseudonym per field (``input_fields``),
so the profiler can tell which field decides an answer. For ``open`` only,
the values of a field that takes few short values (a band, a status, a
flag) are remembered on this machine too (``field_values``); a field with
many values -- ids, emails, free text -- is never remembered.
"""
from __future__ import annotations

import json
import re
from typing import Any

from . import policy
from .pseudonym import Pseudonymizer

KEEPABLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./-]{0,79}")
# a field's values are remembered for ``open`` only while it has at most this
# many distinct ones, each a short single-line scalar
MAX_FIELD_VALUES = 20
MAX_VALUE_CHARS = 40
MODEL = re.compile(r"[a-z0-9][a-z0-9.-]*")


class Names:
    def __init__(self, ps: Pseudonymizer, keep: bool = False) -> None:
        self.ps = ps
        self.keep = keep
        self.kept: set[str] = set()
        self.seen: dict[str, str] = {}
        # field -> {pseudonym: value}, or None once the field has too many values
        self._values: dict[str, dict[str, str] | None] = {}

    def _named(self, domain: str, value: Any) -> str:
        token = self.ps.token(domain, value)
        if domain in ("name", "model") and isinstance(value, str):
            self.seen[token] = value
        return token

    def token(self, domain: str, value: Any) -> str:
        if domain in ("name", "model"):
            return self._named(domain, value)
        return self.ps.token(domain, value)

    def step(self, value: Any, domain: str = "name") -> str:
        """A workflow, agent, node or tool name."""
        if self.keep and isinstance(value, str) and KEEPABLE.fullmatch(value):
            self.kept.add(value)
            return value
        return self._named(domain, value)

    def steps(self, value: str, sep: str) -> str:
        """Several step names joined by ``sep`` (``a+b``, ``a + b``)."""
        return sep.join(self.step(part.strip()) for part in value.split(sep.strip()))

    def input_fields(self, state: dict) -> dict[str, str]:
        """An input, one pseudonym per top-level field. The field name is a
        name (``step``); the value's pseudonym covers the field too, so the
        same value in two fields doesn't match. A nested value is one value."""
        fields = {}
        for key, value in state.items():
            token = self.ps.token("input", [str(key), value])
            fields[self.step(str(key))] = token
            self._remember(str(key), token, value)
        return fields

    def _remember(self, field: str, token: str, value: Any) -> None:
        values = self._values.setdefault(field, {})
        if values is None:
            return
        label = value if isinstance(value, str) else json.dumps(value)
        if not isinstance(value, (str, int, float, bool)) or len(label) > MAX_VALUE_CHARS or "\n" in label:
            self._values[field] = None
            return
        values[token] = label
        if len(values) > MAX_FIELD_VALUES:
            self._values[field] = None

    def field_values(self) -> dict[str, str]:
        """Pseudonym -> value for the fields with few short values."""
        return {t: v for values in self._values.values() if values for t, v in values.items()}

    def model(self, value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, str) and value.startswith(policy.MODEL_PREFIXES) and MODEL.fullmatch(value):
            return value
        return self._named("model", value)


def version(value: Any) -> str | None:
    parts = value.split(".") if isinstance(value, str) else []
    return value if len(parts) == 3 and all(p.isdigit() for p in parts) else None
