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
    proc = subprocess.Popen([sys.executable, "-m", "profiler_client.cli", "app", "--no-browser"],
                            stdout=subprocess.PIPE, text=True, env=env)
    try:
        assert re.search(r"http://127\.0\.0\.1:\d+/\?t=", proc.stdout.readline() + proc.stdout.readline()
                         + proc.stdout.readline())
        assert len(list(tmp_path.glob("profiler-client-app-*"))) == 1
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=30) == 0
    finally:
        proc.kill()
    assert not list(tmp_path.glob("profiler-client-app-*"))
