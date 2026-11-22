"""End-to-end check of the installed ``profiler-client`` command, the way a user runs it.

The unit tests import the package from ``src``. This runs the console script
that ``pip install`` made (on Windows ``profiler-client.exe``) as a separate
process, on a sample trace whose path has spaces and non-ASCII letters, with
the console's own code page, and checks what a user would see:

- ``version``, ``--version``, ``check`` and ``check --json`` work;
- ``prepare`` with ``--key-file`` writes a dataset that ``validate`` and
  ``inspect`` accept, the key file is DPAPI-protected on Windows (hex
  elsewhere), and a second ``prepare`` with the same key file reuses the key;
- the name list and event references are written under ``XDG_DATA_HOME``
  and ``forget --all`` deletes them;
- ``app --no-browser`` serves its page (static files are in the install),
  swaps the start token for a cookie, refuses a request without it, and
  deletes its copied files when stopped with Ctrl-Break (Windows) or SIGTERM.

Run from anywhere after installing the client:

    python scripts/cli_smoke.py

Exits non-zero on the first failure. Uses only the standard library and opens
no connection except to the app it starts on 127.0.0.1.
"""
from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

WINDOWS = os.name == "nt"
SAMPLE = Path(__file__).resolve().parent.parent / "tests" / "data" / "loan-day" / "otlp-traces.jsonl.gz"
DPAPI_HEADER = "profiler-client key dpapi v1"


def fail(message: str) -> None:
    print(f"FAIL: {message}", flush=True)
    sys.exit(1)


def command() -> list[str]:
    exe = shutil.which("profiler-client")
    if not exe:
        fail("profiler-client is not on PATH; install the client first (pip install .)")
    return [exe]


def run(env: dict, *args: str, expect: int = 0) -> subprocess.CompletedProcess:
    # no encoding override for the child: it must cope with the console's code page by itself.
    # Read its output as UTF-8, which the client prints whatever the code page (#54)
    proc = subprocess.run(command() + list(args), env=env, capture_output=True, timeout=300, check=False)
    out = proc.stdout.decode("utf-8", errors="strict")
    err = proc.stderr.decode("utf-8", errors="strict")
    print(f"$ profiler-client {' '.join(args)}  -> exit {proc.returncode}", flush=True)
    if proc.returncode != expect:
        print(out, err, sep="\n")
        fail(f"`{args[0]}` exited {proc.returncode}, expected {expect}")
    if "Traceback" in out + err:
        print(out, err, sep="\n")
        fail(f"`{args[0]}` printed a traceback")
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def check_cli(root: Path, env: dict) -> None:
    trace = root / "my traces ü é" / "otlp traces.jsonl"
    trace.parent.mkdir()
    trace.write_bytes(gzip.decompress(SAMPLE.read_bytes()))

    version = run(env, "version").stdout
    if not re.search(r"profiler-client \d+\.\d+\.\d+", version):
        fail(f"unexpected version output: {version!r}")
    if run(env, "--version").stdout != version:
        fail("--version and version print different things")

    run(env, "check", str(trace))
    summary = json.loads(run(env, "check", "--json", str(trace)).stdout)
    if not isinstance(summary, dict):
        fail("check --json did not print one JSON object")

    key = root / "key ü" / "key"
    out1, out2 = root / "dataset ü 1", root / "dataset ü 2"
    run(env, "prepare", str(trace), "-o", str(out1), "--key-file", str(key))
    events = (out1 / "events.jsonl").read_bytes()
    if b"\r\n" in events:
        fail("events.jsonl has Windows line endings")
    seal = json.loads(events.split(b"\n", 1)[0])["profiler_client_seal"]
    if seal["events"] < 1:
        fail("prepare wrote no events")

    key_text = key.read_text(encoding="ascii")
    if WINDOWS and not key_text.startswith(DPAPI_HEADER):
        fail("the key file on Windows is not DPAPI-protected")
    if not WINDOWS and not re.fullmatch(r"[0-9a-f]{64}\s*", key_text):
        fail("the key file is not a hex key")
    if not WINDOWS and key.stat().st_mode & 0o077:
        fail(f"the key file is mode {oct(key.stat().st_mode & 0o777)}, expected 0600")

    run(env, "validate", str(out1))
    run(env, "inspect", str(out1))

    run(env, "prepare", str(trace), "-o", str(out2), "--key-file", str(key))
    seal2 = json.loads((out2 / "events.jsonl").read_bytes().split(b"\n", 1)[0])["profiler_client_seal"]
    if seal2["key_id"] != seal["key_id"]:
        fail("a second prepare with the same key file used another key")

    data = Path(env["XDG_DATA_HOME"]) / "profiler-client"
    for kind in ("names", "refs"):
        if not (data / kind / f"{seal['payload_sha256']}.json").is_file():
            fail(f"prepare did not write its {kind} file under XDG_DATA_HOME")
    forgot = run(env, "forget", "--all").stdout
    if not re.search(r"removed [1-9]\d* name list", forgot):
        fail(f"forget --all removed nothing: {forgot!r}")
    if list((data / "names").glob("*.json")) or list((data / "refs").glob("*.json")):
        fail("forget --all left files behind")

    # a refused input says so with an error, not a traceback
    run(env, "prepare", str(trace.parent), "-o", str(root / "refused"), expect=1)


def check_app(root: Path, env: dict) -> None:
    tmp = root / "app tmp ü"
    tmp.mkdir()
    env = {**env, "TMPDIR": str(tmp), "TEMP": str(tmp), "TMP": str(tmp)}
    # Ctrl-Break reaches the app only in its own process group; Windows has no SIGTERM to catch
    proc = subprocess.Popen(command() + ["app", "--no-browser"], env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if WINDOWS else 0)
    print("$ profiler-client app --no-browser", flush=True)
    try:
        lines = [proc.stdout.readline().decode("utf-8") for _ in range(3)]
        match = re.search(r"http://127\.0\.0\.1:(\d+)/\?t=\S+", "".join(lines))
        if not match:
            fail(f"app printed no start address: {lines!r}")
        if len(list(tmp.glob("profiler-client-app-*"))) != 1:
            fail("app made no work folder in the temp directory")
        base = f"http://127.0.0.1:{match.group(1)}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

        try:
            opener.open(base + "/", timeout=10)
            fail("app served its page without the start token")
        except urllib.error.HTTPError as exc:
            if exc.code < 400:
                fail(f"app answered {exc.code} without the start token")

        try:
            opener.open(match.group(0), timeout=10)
            fail("the start address did not redirect")
        except urllib.error.HTTPError as exc:
            if exc.code != 303:
                fail(f"the start address answered {exc.code}, expected 303")
            cookie = exc.headers["Set-Cookie"].split(";", 1)[0]
        with opener.open(urllib.request.Request(base + "/", headers={"Cookie": cookie}), timeout=10) as page:
            if page.status != 200 or b"<html" not in page.read().lower():
                fail("app did not serve its page with the cookie")
        print("  page served; token swapped for a cookie; request without it refused", flush=True)

        proc.send_signal(signal.CTRL_BREAK_EVENT if WINDOWS else signal.SIGTERM)
        code = proc.wait(timeout=30)
        if code != 0:
            fail(f"app exited {code} when stopped")
    finally:
        if proc.poll() is None:
            proc.kill()
    if list(tmp.glob("profiler-client-app-*")):
        fail("app left its copied files behind after stopping")
    print("  stopped cleanly and deleted its work folder", flush=True)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def main() -> int:
    print(f"platform: {sys.platform}, stdout encoding: {sys.stdout.encoding}, "
          f"filesystem encoding: {sys.getfilesystemencoding()}", flush=True)
    # this script's own log keeps the console's code page (what it reports above), but a path it can't
    # encode there, like "data ü" on a Hebrew or Russian Windows (cp1255, cp1251), prints escaped, not a crash
    sys.stdout.reconfigure(errors="backslashreplace")
    sys.stderr.reconfigure(errors="backslashreplace")
    root = Path(tempfile.mkdtemp(prefix="profiler-client-smoke-"))
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    env["XDG_DATA_HOME"] = str(root / "data ü")
    try:
        check_cli(root, env)
        check_app(root, env)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("OK: the installed client behaves as expected", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
