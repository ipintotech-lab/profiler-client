import subprocess
import sys
from pathlib import Path

import pytest

from profiler_client import __version__, cli

CHECK = Path(__file__).resolve().parents[1] / "scripts" / "check_version.py"


def test_version_flag_prints_what_the_version_command_prints(capsys):
    assert cli.main(["version"]) == 0
    command = capsys.readouterr().out
    with pytest.raises(SystemExit) as stop:
        cli.main(["--version"])
    assert stop.value.code == 0
    assert capsys.readouterr().out == command
    assert command.startswith(f"profiler-client {__version__}\n")


def _check(*args):
    return subprocess.run([sys.executable, str(CHECK), *args], capture_output=True, text=True, check=False)


def test_the_package_says_one_version_and_a_tag_must_name_it():
    assert _check().returncode == 0
    assert _check(f"v{__version__}").returncode == 0
    wrong = _check("v0.0.1")
    assert wrong.returncode == 1 and f"it should be v{__version__}" in wrong.stderr
