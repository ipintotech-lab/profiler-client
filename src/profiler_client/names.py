"""Names a converter writes: pseudonyms by default, and with ``--keep-names``
the step names the user chose -- workflow, agent, node and tool names --
in clear text.

Only name-like strings are kept: one word of ``[A-Za-z0-9_./-]``, at most
80 characters. A sentence, a command or anything with spaces stays a
pseudonym even with ``--keep-names``. Every kept name is recorded in the
manifest, so ``inspect`` shows exactly which ones would leave the machine,
and the lint accepts only those.

Contents -- inputs, outputs, ids, commands, script names -- are never kept.
"""
from __future__ import annotations

import re
from typing import Any

from . import policy
from .pseudonym import Pseudonymizer

KEEPABLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./-]{0,79}")
MODEL = re.compile(r"[a-z0-9][a-z0-9.-]*")


class Names:
    def __init__(self, ps: Pseudonymizer, keep: bool = False) -> None:
        self.ps = ps
        self.keep = keep
        self.kept: set[str] = set()

    def token(self, domain: str, value: Any) -> str:
        return self.ps.token(domain, value)

    def step(self, value: Any, domain: str = "name") -> str:
        """A workflow, agent, node or tool name."""
        if self.keep and isinstance(value, str) and KEEPABLE.fullmatch(value):
            self.kept.add(value)
            return value
        return self.ps.token(domain, value)

    def steps(self, value: str, sep: str) -> str:
        """Several step names joined by ``sep`` (``a+b``, ``a + b``)."""
        return sep.join(self.step(part.strip()) for part in value.split(sep.strip()))

    def model(self, value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, str) and value.startswith(policy.MODEL_PREFIXES) and MODEL.fullmatch(value):
            return value
        return self.ps.token("model", value)


def version(value: Any) -> str | None:
    parts = value.split(".") if isinstance(value, str) else []
    return value if len(parts) == 3 and all(p.isdigit() for p in parts) else None
