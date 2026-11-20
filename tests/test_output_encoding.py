"""The commands print UTF-8 whatever the console's code page.

Step names hold characters such as ``←`` (``llm_call ← Bash``). On Windows,
output sent to a pipe or file uses the locale's code page (cp1252, cp1255,
...), which can't encode them, so ``inspect`` used to fail there.
``PYTHONIOENCODING`` forces such a code page on any OS, so this runs in CI.
"""
import os
import subprocess
import sys

import pytest


def _run(*args, encoding):
    env = {**os.environ, "PYTHONIOENCODING": encoding}
    env.pop("PYTHONUTF8", None)
    return subprocess.run([sys.executable, "-m", "profiler_client.cli", *map(str, args)],
                          capture_output=True, env=env, check=False)


@pytest.mark.parametrize("encoding", ["cp1255", "cp1252", "ascii"])
def test_prepare_and_inspect_print_utf8_to_a_pipe(tmp_path, transcript_file, encoding):
    out = tmp_path / "ready"
    prepared = _run("prepare", transcript_file, "-o", out, encoding=encoding)
    assert prepared.returncode == 0, prepared.stderr.decode("utf-8", "replace")

    inspected = _run("inspect", out, encoding=encoding)
    assert inspected.returncode == 0, inspected.stderr.decode("utf-8", "replace")
    # the names that broke it are printed, as UTF-8
    assert "llm_call ←" in inspected.stdout.decode("utf-8")
