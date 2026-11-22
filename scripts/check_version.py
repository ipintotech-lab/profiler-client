"""Check that the package says one version everywhere, and that a release tag
names that version.

    python scripts/check_version.py            # pyproject.toml, __init__.py and the README install line agree
    python scripts/check_version.py v0.37.1    # ... and the tag is v<that version>

CI runs it on every push and pull request, and on every pushed v* tag. A tag
can't be stopped from being pushed, so a tag that fails here has to be
replaced by a new version."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _one(pattern: str, path: str) -> str:
    found = set(re.findall(pattern, (ROOT / path).read_text(encoding="utf-8"), re.MULTILINE))
    if len(found) != 1:
        sys.exit(f"error: {path}: expected one version, found {sorted(found) or 'none'}")
    return found.pop()


def main(argv: list[str]) -> int:
    versions = {
        "pyproject.toml": _one(r'^version = "(\d+\.\d+\.\d+)"$', "pyproject.toml"),
        "src/profiler_client/__init__.py": _one(r'^__version__ = "(\d+\.\d+\.\d+)"$', "src/profiler_client/__init__.py"),
        "README.md install": _one(r"profiler-client/archive/refs/tags/v(\d+\.\d+\.\d+)\.tar\.gz", "README.md"),
    }
    if len(set(versions.values())) != 1:
        sys.exit("error: the versions disagree: " + ", ".join(f"{k} {v}" for k, v in versions.items()))
    version = versions["pyproject.toml"]
    if argv and argv[0] != f"v{version}":
        sys.exit(f"error: tag {argv[0]} doesn't match the package version {version}; it should be v{version}")
    print(f"version {version}" + (f", tag {argv[0]}" if argv else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
