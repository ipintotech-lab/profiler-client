"""The app's working folders: each records the process that made it, so a
folder left behind by an app that was killed can be deleted the next time
the app starts, and a folder of an app that is still running never is (#58).

A process is told by its id and its start time, so an id that a new process
reuses doesn't keep a stale folder. A folder with no record, or one that
can't be read, is left alone and reported with the command to delete it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PREFIX = "profiler-client-app-"
RECORD = "process.json"


def started(pid: int) -> str | None:
    """When process ``pid`` started, as an opaque string, or None when no
    such process is running. Raises OSError when it can't be told."""
    if sys.platform == "win32":
        return _started_windows(pid)
    stat = Path(f"/proc/{pid}/stat")
    if Path("/proc/self/stat").exists():  # Linux
        try:
            fields = stat.read_text().rpartition(")")[2].split()
        except FileNotFoundError:
            return None
        return None if fields[0] == "Z" else fields[19]  # state, ..., starttime (field 22)
    run = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], check=False, capture_output=True, text=True,
                         env={**os.environ, "LC_ALL": "C"}, timeout=10)  # macOS and other Unix
    return run.stdout.strip() or None


def _started_windows(pid: int) -> str | None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        if ctypes.get_last_error() == 87:  # ERROR_INVALID_PARAMETER: no process with that id
            return None
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        if code.value != 259:  # not STILL_ACTIVE: it has ended
            return None
        created, ended, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in (created, ended, kernel, user))):
            raise ctypes.WinError(ctypes.get_last_error())
        return str(created.dwHighDateTime << 32 | created.dwLowDateTime)
    finally:
        kernel32.CloseHandle(handle)


def make() -> Path:
    """A new working folder (mode 0700) that records this process."""
    workdir = Path(tempfile.mkdtemp(prefix=PREFIX))
    (workdir / RECORD).write_text(json.dumps({"pid": os.getpid(), "started": started(os.getpid())}),
                                  encoding="utf-8")
    return workdir


def delete_command(path: Path) -> str:
    return f'rmdir /s /q "{path}"' if sys.platform == "win32" else f"rm -rf '{path}'"


def sweep(folder: Path | None = None) -> list[Path]:
    """Delete the working folders in ``folder`` (the temp folder) whose app
    is no longer running; report the ones that can't be told. Returns the
    folders deleted."""
    deleted = []
    for workdir in sorted(Path(folder or tempfile.gettempdir()).glob(PREFIX + "*")):
        if workdir.is_symlink() or not workdir.is_dir():
            continue
        if hasattr(os, "getuid") and workdir.stat().st_uid != os.getuid():
            continue  # another user's
        try:
            record = json.loads((workdir / RECORD).read_text(encoding="utf-8"))
            if not isinstance(record["started"], str):
                raise TypeError
            running = started(int(record["pid"])) == record["started"]
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            print(f"A working folder no app can be matched to was left alone; if no profiler-client app is "
                  f"running, delete it: {delete_command(workdir)}", flush=True)
            continue
        if running:
            continue
        shutil.rmtree(workdir, ignore_errors=True)
        if workdir.exists():
            print(f"A working folder from an app that stopped could not be deleted; delete it: "
                  f"{delete_command(workdir)}", flush=True)
        else:
            deleted.append(workdir)
    return deleted
