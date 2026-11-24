"""``profiler-client app``: the local page, its gate and its round trip to a
stand-in Openly7 server."""

import json
import os
import re
import signal
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from conftest import CANARIES

from profiler_client import app
from profiler_client.dataset import read_sealed

CLIENT_TOKEN = "pc_" + "k" * 40


class Openly7Stub(BaseHTTPRequestHandler):
    """Takes one upload, queues one overview job and answers with a report
    naming the dataset's workflow by its pseudonym."""

    def log_message(self, *args):
        pass

    def _json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self):
        stub = self.server
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        stub.seen.append({"method": self.command, "path": self.path, "headers": dict(self.headers), "body": body})
        if self.headers.get("Authorization") != f"Bearer {CLIENT_TOKEN}":
            return self._json(401, {"detail": "not signed in"})
        if stub.redirect:
            self.send_response(302)
            self.send_header("Location", stub.redirect)
            self.send_header("Content-Length", "0")
            return self.end_headers()
        if (self.command, self.path) == ("POST", "/api/files"):
            payload = body.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]
            stub.seal, stub.events = read_sealed(payload)
            return self._json(201, {"files": [{"id": "f1", "status": "READY"}]})
        if (self.command, self.path) == ("POST", "/api/files/f1/jobs"):
            stub.job = json.loads(body)
            return self._json(202, {"id": "j1", "status": "QUEUED"})
        if (self.command, self.path) == ("GET", "/api/jobs/j1"):
            stub.polls += 1
            done = stub.polls > 1
            return self._json(200, {"id": "j1", "status": "COMPLETED" if done else "RUNNING",
                                    "result_id": "r1" if done else None, "error": None})
        if (self.command, self.path) == ("GET", "/api/results/r1"):
            workflow = stub.events[0]["workflow"]
            # every pseudonym the dataset holds, as the report's steps
            tokens = sorted(set(re.findall(r"h:[0-9a-f]{64}", json.dumps(stub.events))))
            return self._json(200, {
                "payload_sha256": stub.seal["payload_sha256"],
                "observe": {"selected_workflow": workflow, "workflows": [{"name": workflow, "num_runs": 1}]},
                "summary": {"totals": {"cost": 1, "runs": 1, "llm_calls": 0, "tool_calls": 1, "tokens": 0}},
                "mine": {"by_agent": {"agent": {"actions": [
                    {"node": t, "occurrences": 1, "total_cost": 1} for t in tokens]}}},
            })
        return self._json(404, {"detail": "not found"})

    do_GET = do_POST = _handle


@pytest.fixture
def openly7():
    stub = ThreadingHTTPServer(("127.0.0.1", 0), Openly7Stub)
    stub.seen, stub.polls, stub.redirect, stub.job = [], 0, None, None
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    yield stub
    stub.shutdown()
    stub.server_close()


@pytest.fixture
def local(tmp_path, monkeypatch, openly7):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    server = app.AppServer(0, app.Session(work, f"http://127.0.0.1:{openly7.server_port}"))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def request(server, method, path, body=None, *, cookie=True, host=None, origin=True, content_type=None):
    host = host or f"127.0.0.1:{server.server_port}"
    headers = {"Host": host}
    if cookie:
        headers["Cookie"] = f"{app.COOKIE}={server.token}"
    if origin and method != "GET":
        headers["Origin"] = f"http://{host}"
    if isinstance(body, dict):
        body, content_type = json.dumps(body).encode(), "application/json"
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}{path}", data=body, method=method,
                                 headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=30) as res:
            return res.status, dict(res.headers), res.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def run_through(local, transcript_file, options=None):
    assert request(local, "PUT", f"/api/raw?name={transcript_file.name}", transcript_file.read_bytes())[0] == 200
    code, _, body = request(local, "POST", "/api/prepare", options or {})
    assert code == 200, body
    code, _, body = request(local, "POST", "/api/submit", {"token": CLIENT_TOKEN, "prices": [1.25, 10]})
    assert code == 200, body
    statuses = [json.loads(request(local, "GET", "/api/status")[2]) for _ in range(2)]
    assert statuses[-1] == {"status": "COMPLETED", "report": True}
    return json.loads(request(local, "GET", "/api/status")[2])


def test_it_listens_on_this_machine_only(local):
    assert local.server_address[0] == "127.0.0.1"
    assert re.fullmatch(r"http://127\.0\.0\.1:\d+/\?t=[A-Za-z0-9_-]{43}", local.url)


def test_the_start_address_swaps_its_token_for_a_strict_cookie(local):
    code, headers, _ = request(local, "GET", f"/?t={local.token}", cookie=False)
    assert code == 303 and headers["Location"] == "/"
    cookie = headers["Set-Cookie"]
    assert cookie.startswith(f"{app.COOKIE}={local.token};")
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    assert request(local, "GET", "/?t=wrong", cookie=False)[0] == 403


@pytest.mark.parametrize("path", ["/", "/report", "/report.json", "/api/status"])
def test_nothing_is_served_without_the_session_cookie(local, path):
    assert request(local, "GET", path, cookie=False)[0] == 403
    code, headers, page = request(local, "GET", "/")
    assert code == 200 and b"Include a fingerprint of what the model read (no text is sent)" in page
    assert headers["X-Frame-Options"] == "DENY" and "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert headers["Cache-Control"] == "no-store"


def test_another_host_name_is_refused_even_with_the_cookie(local):
    # a page that rebinds its own domain to 127.0.0.1 would send its own Host
    assert request(local, "GET", "/", host=f"evil.example:{local.server_port}")[0] == 403
    assert request(local, "GET", f"/?t={local.token}", cookie=False, host="evil.example")[0] == 403


def test_changes_need_this_page_as_their_origin(local, transcript_file):
    assert request(local, "POST", "/api/prepare", {}, origin=False)[0] == 403
    status, _, _ = request(local, "PUT", "/api/raw?name=a.jsonl", transcript_file.read_bytes(),
                           host=f"127.0.0.1:{local.server_port}", origin=False)
    assert status == 403
    req_headers = {"Host": f"127.0.0.1:{local.server_port}", "Origin": "http://evil.example",
                   "Cookie": f"{app.COOKIE}={local.token}", "Content-Type": "application/json"}
    req = urllib.request.Request(f"http://127.0.0.1:{local.server_port}/api/prepare", data=b"{}", method="POST",
                                 headers=req_headers)
    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(req, timeout=30)
    assert refused.value.code == 403


def test_names_are_pseudonyms_and_the_llm_prompt_is_left_out_by_default(local, transcript_file, monkeypatch):
    used = {}
    real = app.prepare_dataset

    def spy(paths, output, **options):
        used.update(options)
        return real(paths, output, **options)

    monkeypatch.setattr(app, "prepare_dataset", spy)
    request(local, "PUT", f"/api/raw?name={transcript_file.name}", transcript_file.read_bytes())
    code, _, body = request(local, "POST", "/api/prepare", {})
    assert code == 200
    assert used == {"keep_names": False, "llm_inputs": False}
    assert json.loads(body)["kept_names"] == []
    request(local, "PUT", f"/api/raw?name={transcript_file.name}", transcript_file.read_bytes())
    request(local, "POST", "/api/prepare", {"keep_names": True, "llm_inputs": True})
    assert used == {"keep_names": True, "llm_inputs": True}


@pytest.mark.parametrize("keep_names", [False, True])
@pytest.mark.parametrize("llm_inputs", [False, True])
def test_the_send_tiles_match_the_seal_and_the_events_file(local, transcript_file, keep_names, llm_inputs):
    """What the send screen says will be sent, counted again here from the
    bytes of events.jsonl: the seal line, then one event per line."""
    request(local, "PUT", f"/api/raw?name={transcript_file.name}", transcript_file.read_bytes())
    code, _, body = request(local, "POST", "/api/prepare", {"keep_names": keep_names, "llm_inputs": llm_inputs})
    assert code == 200, body
    out = json.loads(body)
    first, *lines = local.session.dataset.read_bytes().splitlines()
    seal, events = json.loads(first)["profiler_client_seal"], [json.loads(line) for line in lines]
    assert out["sent"] == {
        "text_fields": 0,
        "clear_names": seal["kept_names"],
        "pseudonyms": len(set(re.findall(rb"h:[0-9a-f]{64}", b"\n".join(lines)))),
        "events": len(events),
        "fingerprints": sum(1 for e in events if e["event_type"] == "decision" and "input" in e.get("input_state", {})),
    }
    assert bool(out["sent"]["clear_names"]) is keep_names
    assert bool(out["sent"]["fingerprints"]) is llm_inputs
    assert out["sent"]["pseudonyms"] > 0 and out["sent"]["events"] == seal["events"] == out["events"]
    body = b"\n".join(lines).decode()
    # the transcript's prompt, reply, paths and commands; only the names the seal lists may be in clear text
    assert not [c for c in CANARIES if c not in seal["kept_names"] and c in body]
    assert out["trust"] == app.trust_line(keep_names, llm_inputs)
    assert ("names as they are" in out["trust"]) is keep_names and ("fingerprint" in out["trust"]) is llm_inputs
    assert "anonym" not in out["trust"].lower()


def test_the_page_opens_with_the_default_trust_line_and_a_link_to_what_is_sent():
    page = (app.files("profiler_client") / "static/app.html").read_text()
    assert f'<p class="trust" id="trust">{app.trust_line()}</p>' in page
    assert 'docs/what-is-sent.md" target="_blank" rel="noopener">Exactly what is sent</a>' in page


def test_the_report_comes_back_with_real_names(local, openly7, transcript_file, tmp_path):
    run_through(local, transcript_file)
    upload, job = openly7.seen[0], openly7.seen[1]
    assert upload["path"] == "/api/files" and len(upload["headers"]["Idempotency-Key"]) == 32
    assert job["headers"]["Idempotency-Key"] != upload["headers"]["Idempotency-Key"]
    assert openly7.job == {"kind": "overview", "token_prices": [1.25, 10.0]}
    for canary in CANARIES:
        assert canary.encode() not in upload["body"]

    code, headers, page = request(local, "GET", "/report")
    assert code == 200 and headers["Content-Type"].startswith("text/html")
    assert "mcp__acme_internal__lookup" in page.decode()
    code, headers, raw = request(local, "GET", "/report.json")
    assert code == 200 and "attachment" in headers["Content-Disposition"]
    assert b"mcp__acme_internal__lookup" not in raw  # the saved JSON keeps pseudonyms

    # the client token went to the server only: nothing on disk holds it
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert CLIENT_TOKEN.encode() not in path.read_bytes(), path


@pytest.fixture
def priced(tmp_path, monkeypatch, openly7):
    """The app as ``app --prices-file`` starts it: a public id, a private one and a default."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    prices = {"claude-opus-4-1": (15.0, 75.0), "acme-llm-v3": (1.0, 2.0), "*": (3.0, 15.0)}
    server = app.AppServer(0, app.Session(work, f"http://127.0.0.1:{openly7.server_port}", prices))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def test_prices_file_prices_go_keyed_as_the_dataset_names_the_models(priced, openly7, tmp_path, transcript_rows):
    rows = [dict(r) for r in transcript_rows]
    for r in rows[2:]:  # one private model id next to the public one
        if r["type"] == "assistant":
            r["message"] = {**r["message"], "model": "acme-llm-v3"}
            break
    trace = tmp_path / "session.jsonl"
    trace.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    assert request(priced, "PUT", f"/api/raw?name={trace.name}", trace.read_bytes())[0] == 200
    code, _, body = request(priced, "POST", "/api/prepare", {})
    assert code == 200 and json.loads(body)["prices_file"] == ["*", "acme-llm-v3", "claude-opus-4-1"]
    code, _, body = request(priced, "POST", "/api/submit", {"token": CLIENT_TOKEN, "prices": [1, 2]})
    assert code == 400 and b"--prices-file" in body  # the page's boxes don't mix with the file
    code, _, body = request(priced, "POST", "/api/submit", {"token": CLIENT_TOKEN, "prices": None})
    assert code == 200, body

    models = {e["model"] for e in openly7.events if e.get("model")}
    (private,) = models - {"claude-opus-4-1"}
    assert "acme" not in private
    assert openly7.job == {"kind": "overview", "token_prices": {
        "claude-opus-4-1": [15.0, 75.0], private: [1.0, 2.0], "*": [3.0, 15.0]}}
    assert b"acme-llm-v3" not in openly7.seen[0]["body"] and b"acme-llm-v3" not in json.dumps(openly7.job).encode()


def test_a_bad_prices_file_stops_the_app_before_it_opens(tmp_path, capsys):
    from profiler_client import cli
    bad = tmp_path / "p.json"
    bad.write_text('{"gpt-5": [1, 2000000]}', encoding="utf-8")
    assert cli.main(["app", "--no-browser", "--prices-file", str(bad)]) == 1
    assert "over 1e+06" in capsys.readouterr().err


def test_the_files_copied_in_are_deleted_once_prepared(local, transcript_file):
    request(local, "PUT", f"/api/raw?name=../../{transcript_file.name}", transcript_file.read_bytes())
    copied = list(local.session.raw.rglob("*.jsonl"))
    assert [p.name for p in copied] == [transcript_file.name]
    assert copied[0].is_relative_to(local.session.raw)
    request(local, "POST", "/api/prepare", {})
    assert not local.session.raw.exists()


def test_a_refused_token_is_said_plainly(local, transcript_file):
    request(local, "PUT", f"/api/raw?name={transcript_file.name}", transcript_file.read_bytes())
    request(local, "POST", "/api/prepare", {})
    code, _, body = request(local, "POST", "/api/submit", {"token": "pc_wrong"})
    assert code == 400 and "refused the client token" in json.loads(body)["error"]
    code, _, body = request(local, "POST", "/api/submit", {"token": "not-a-token"})
    assert code == 400 and "pc_" in json.loads(body)["error"]


def test_a_redirect_does_not_carry_the_token_elsewhere(local, openly7, transcript_file):
    openly7.redirect = "http://127.0.0.1:9/elsewhere"
    request(local, "PUT", f"/api/raw?name={transcript_file.name}", transcript_file.read_bytes())
    request(local, "POST", "/api/prepare", {})
    code, _, body = request(local, "POST", "/api/submit", {"token": CLIENT_TOKEN})
    assert code == 400 and "redirect" in json.loads(body)["error"]
    assert len(openly7.seen) == 1


@pytest.mark.parametrize("url", ["http://openly7.com", "ftp://openly7.com", "https://openly7.com/app",
                                 "https://user@openly7.com", "openly7.com"])
def test_the_server_must_be_https_or_this_machine(url):
    with pytest.raises(ValueError):
        app.server_origin(url)


def test_server_origin_accepts_the_site_and_a_local_check():
    assert app.server_origin("https://openly7.com/") == "https://openly7.com"
    assert app.server_origin("http://localhost:8767") == "http://localhost:8767"


def test_stopping_the_app_deletes_the_files_it_copied(tmp_path):
    env = {**os.environ, "TMPDIR": str(tmp_path), "XDG_DATA_HOME": str(tmp_path / "data")}
    # Windows has no SIGTERM to catch (send_signal(SIGTERM) ends the process outright); Ctrl-Break
    # arrives as SIGBREAK, which needs the app in its own process group. Closing the console window
    # is the next test.
    windows = os.name == "nt"
    proc = subprocess.Popen([sys.executable, "-m", "profiler_client.cli", "app", "--no-browser"],
                            stdout=subprocess.PIPE, text=True, env=env, encoding="utf-8",
                            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if windows else 0)
    try:
        assert re.search(r"http://127\.0\.0\.1:\d+/\?t=", proc.stdout.readline() + proc.stdout.readline()
                         + proc.stdout.readline())
        assert len(list(tmp_path.glob("profiler-client-app-*"))) == 1
        proc.send_signal(signal.CTRL_BREAK_EVENT if windows else signal.SIGTERM)
        assert proc.wait(timeout=30) == 0
    finally:
        proc.kill()
    assert not list(tmp_path.glob("profiler-client-app-*"))


def test_a_killed_apps_files_are_deleted_when_the_app_next_starts(tmp_path):
    # killed outright (SIGKILL, or TerminateProcess on Windows): no handler runs, so the folder stays
    env = {**os.environ, "TMPDIR": str(tmp_path), "TEMP": str(tmp_path), "TMP": str(tmp_path),
           "XDG_DATA_HOME": str(tmp_path / "data")}

    def start():
        proc = subprocess.Popen([sys.executable, "-m", "profiler_client.cli", "app", "--no-browser"],
                                stdout=subprocess.PIPE, text=True, env=env, encoding="utf-8")
        assert re.search(r"http://127\.0\.0\.1:\d+/\?t=", proc.stdout.readline() + proc.stdout.readline()
                         + proc.stdout.readline())
        return proc

    first = start()
    [left] = tmp_path.glob("profiler-client-app-*")
    first.kill()
    first.wait(timeout=30)
    assert left.exists()
    second = start()
    try:
        [workdir] = tmp_path.glob("profiler-client-app-*")
        assert workdir != left and not left.exists()
    finally:
        second.kill()
        second.wait(timeout=30)


@pytest.mark.skipif(os.name == "nt", reason="a process can't be sent SIGINT on Windows; Ctrl+C there is checked by hand (#58)")
def test_ctrl_c_deletes_the_files_it_copied(tmp_path):
    # Ctrl+C is SIGINT, which Python's own handler raises as KeyboardInterrupt: not the SIGTERM
    # handler the test above goes through
    env = {**os.environ, "TMPDIR": str(tmp_path), "XDG_DATA_HOME": str(tmp_path / "data")}
    # a process started in the background ignores SIGINT, and its child inherits that; put Python's
    # own Ctrl+C handler back in the child before the app starts, as a terminal would have it
    start = ("import runpy, signal, sys; signal.signal(signal.SIGINT, signal.default_int_handler); "
             "sys.argv = ['profiler-client', 'app', '--no-browser']; "
             "runpy.run_module('profiler_client.cli', run_name='__main__', alter_sys=True)")
    proc = subprocess.Popen([sys.executable, "-c", start], stdout=subprocess.PIPE, text=True, env=env, encoding="utf-8")
    try:
        assert re.search(r"http://127\.0\.0\.1:\d+/\?t=", proc.stdout.readline() + proc.stdout.readline()
                         + proc.stdout.readline())
        [workdir] = tmp_path.glob("profiler-client-app-*")
        (workdir / "raw").mkdir(exist_ok=True)
        (workdir / "raw" / "session.jsonl").write_text("{}\n", encoding="utf-8")  # a file it copied in
        proc.send_signal(signal.SIGINT)
        assert proc.wait(timeout=30) == 0
    finally:
        proc.kill()
    assert not workdir.exists()


@pytest.mark.skipif(os.name != "nt", reason="console close events exist on Windows only")
def test_on_windows_closing_the_console_window_deletes_the_files_it_copied(tmp_path):
    # Windows can't send CTRL_CLOSE_EVENT to a test, so call the handler the app registers
    # as Windows would; it must delete the folder itself, since the process ends when it returns
    import ctypes
    workdir = tmp_path / "profiler-client-app-x"
    workdir.mkdir()
    (workdir / "trace.jsonl").write_text("{}", encoding="utf-8")
    handler = app.delete_on_console_close(workdir)
    try:
        assert handler(0) == 0 and handler(1) == 0  # Ctrl-C and Ctrl-Break go on to Python
        assert workdir.exists()
        assert handler(2) == 0  # CTRL_CLOSE_EVENT
        assert not workdir.exists()
    finally:
        ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, False)
