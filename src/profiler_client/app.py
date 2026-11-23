"""``profiler-client app``: a page on this machine that prepares trace files,
sends the dataset to Openly7 and shows the report with your names restored.

The page is served on 127.0.0.1 only, on a port picked at start. Opening the
address printed at start (``/?t=<token>``) swaps the one-time token in the
address for a cookie; every other request needs that cookie, a ``Host`` of
this address (so a web page that rebinds a domain to 127.0.0.1 is refused) and,
to change anything, an ``Origin`` of this page. Your trace files are copied to
a temporary folder that is deleted when the app stops. The name list and
event references are kept as ``prepare`` keeps them, so ``open`` and
``show-event`` work on the report later. The Openly7 client token you type is
kept in memory only.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import shutil
import signal
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path

from .cli import parse_report, prepare_check, prepare_dataset, report_title, restore_names
from .dataset import EVENTS, PrepareError
from .report import render_html

COOKIE = "pc_session"
MAX_RAW_BYTES = 2 * 1024**3  # all trace files of one dataset, as copied to the temporary folder
MAX_JSON_BYTES = 64 * 1024
# a refused request's body up to this size is read and dropped before the refusal is sent: closing with unread
# data resets the connection on Windows, and the sender then sees a network error instead of the 403
MAX_DISCARD_BYTES = 1024 * 1024
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
CLIENT_TOKEN = re.compile(r"pc_[A-Za-z0-9_-]{1,200}")
PAGE_CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; "
            "img-src data:; font-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


class RemoteError(Exception):
    """The Openly7 site refused a request or couldn't be reached."""


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    # a redirect would resend the client token to wherever it points
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RemoteError(f"the server answered with a redirect ({code}); check --server")


def server_origin(url: str) -> str:
    """``url`` as an origin to send to: https, or http to this machine."""
    parts = urllib.parse.urlsplit(url.strip())
    if parts.scheme not in ("https", "http") or not parts.hostname or parts.path not in ("", "/") \
            or parts.query or parts.fragment or parts.username or parts.password:
        raise ValueError(f"--server must be a site address like https://openly7.com, not {url!r}")
    if parts.scheme == "http" and parts.hostname not in LOCAL_HOSTS:
        raise ValueError("--server must use https unless it is on this machine")
    return f"{parts.scheme}://{parts.netloc}"


def safe_name(name: str) -> str:
    """A file's base name, as a single safe path part."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    base = re.sub(r"[^A-Za-z0-9._ -]", "_", base)[:200].strip()
    if base in ("", ".", ".."):
        raise ValueError("a file needs a name")
    return base


class Openly7:
    """The few calls to the Openly7 API the app makes, with a client token."""

    def __init__(self, origin: str, token: str) -> None:
        self.origin, self.token = origin, token
        self._open = urllib.request.build_opener(_NoRedirects).open

    def _call(self, method: str, path: str, body: bytes | None = None, content_type: str | None = None,
              idempotent: bool = False) -> bytes:
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        if idempotent:
            headers["Idempotency-Key"] = secrets.token_hex(16)
        req = urllib.request.Request(self.origin + path, data=body, method=method, headers=headers)
        try:
            with self._open(req, timeout=300) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise RemoteError(_refusal(exc)) from None
        except urllib.error.URLError as exc:
            raise RemoteError(f"could not reach {self.origin}: {exc.reason}") from None

    def upload(self, dataset: Path) -> dict:
        boundary = secrets.token_hex(16)
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{EVENTS}"\r\n'
                "Content-Type: application/x-ndjson\r\n\r\n").encode() + dataset.read_bytes() \
            + f"\r\n--{boundary}--\r\n".encode()
        (stored,) = json.loads(self._call("POST", "/api/files", body, f"multipart/form-data; boundary={boundary}",
                                          idempotent=True))["files"]
        return stored

    def start_overview(self, file_id: str, prices: list[float] | None) -> dict:
        params: dict = {"kind": "overview"}
        if prices:
            params["token_prices"] = prices
        return json.loads(self._call("POST", f"/api/files/{urllib.parse.quote(file_id)}/jobs",
                                     json.dumps(params).encode(), "application/json", idempotent=True))

    def job(self, job_id: str) -> dict:
        return json.loads(self._call("GET", f"/api/jobs/{urllib.parse.quote(job_id)}"))

    def result(self, result_id: str) -> bytes:
        return self._call("GET", f"/api/results/{urllib.parse.quote(result_id)}")


def _refusal(exc: urllib.error.HTTPError) -> str:
    if exc.code == 401:
        return "Openly7 refused the client token; make a new one on the site and try again"
    try:
        detail = json.loads(exc.read(MAX_JSON_BYTES)).get("detail")
    except (ValueError, AttributeError):
        detail = None
    if isinstance(detail, dict):
        detail = detail.get("message")
    return f"Openly7 answered {exc.code}" + (f": {detail}" if isinstance(detail, str) else "")


class Session:
    """What one run of the app holds: the files chosen, the dataset made
    from them, the job and the report. One user, so one lock."""

    def __init__(self, workdir: Path, origin: str) -> None:
        self.lock = threading.Lock()
        self.workdir, self.origin = workdir, origin
        self.raw = workdir / "raw"
        self.raw_bytes = 0
        self.datasets = 0
        self.dataset: Path | None = None
        self.remote: Openly7 | None = None
        self.job_id: str | None = None
        self.report_json: bytes | None = None
        self.report_html: str | None = None

    def clear_raw(self) -> None:
        shutil.rmtree(self.raw, ignore_errors=True)
        self.raw_bytes = 0

    def add_raw(self, name: str, length: int, body) -> None:
        if length > MAX_RAW_BYTES - self.raw_bytes:
            raise ValueError(f"the files chosen are larger than {MAX_RAW_BYTES // 1024**3} GB in all")
        index = sum(1 for _ in self.raw.iterdir()) if self.raw.exists() else 0
        folder = self.raw / f"{index:04d}"  # its own folder, so two files can share a name
        folder.mkdir(parents=True, mode=0o700)
        with open(folder / safe_name(name), "xb") as out:
            left = length
            while left:
                chunk = body.read(min(left, 1024 * 1024))
                if not chunk:
                    raise ValueError("the file was cut short")
                out.write(chunk)
                left -= len(chunk)
        self.raw_bytes += length

    def prepare(self, keep_names: bool, llm_inputs: bool) -> dict:
        paths = sorted(p for p in self.raw.glob("*/*")) if self.raw.exists() else []
        if not paths:
            raise ValueError("choose at least one trace file")
        self.datasets += 1
        output = self.workdir / f"dataset-{self.datasets}"
        try:
            manifest, events = prepare_dataset(paths, output, keep_names=keep_names, llm_inputs=llm_inputs)
        finally:
            self.clear_raw()
        self.dataset, self.job_id, self.report_json, self.report_html = output / EVENTS, None, None, None
        return {"check": prepare_check(manifest, events, len(paths)), "events": manifest["events"],
                "sessions": manifest["sessions"], "kept_names": manifest["kept_names"]}

    def submit(self, token: str, prices: list[float] | None) -> dict:
        if self.dataset is None:
            raise ValueError("prepare the files first")
        if not CLIENT_TOKEN.fullmatch(token):
            raise ValueError("a client token starts with pc_; make one on the Openly7 site")
        self.remote = Openly7(self.origin, token)
        stored = self.remote.upload(self.dataset)
        if stored.get("status") != "READY":
            raise RemoteError(f"Openly7 did not accept the dataset: {stored.get('reason') or stored.get('status')}")
        job = self.remote.start_overview(stored["id"], prices)
        self.job_id, self.report_json, self.report_html = job["id"], None, None
        return {"status": job["status"]}

    def status(self) -> dict:
        if self.report_html is not None:
            return {"status": "COMPLETED", "report": True}
        if self.remote is None or self.job_id is None:
            return {"status": None}
        job = self.remote.job(self.job_id)
        if job["status"] == "COMPLETED" and job.get("result_id"):
            raw = self.remote.result(job["result_id"])
            report, found, _, _, _ = restore_names(parse_report(raw))
            self.report_json = raw
            self.report_html = render_html(report, report_title(report, "Your report"),
                                           restored=len(found))
            return {"status": "COMPLETED", "report": True}
        return {"status": job["status"], "error": job.get("error")}


class AppServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, session: Session) -> None:
        super().__init__(("127.0.0.1", port), Handler)
        self.session = session
        self.token = secrets.token_urlsafe(32)
        self.hosts = {f"127.0.0.1:{self.server_port}", f"localhost:{self.server_port}"}

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}/?t={self.token}"


class Handler(BaseHTTPRequestHandler):
    server: AppServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # the start address holds the token; log nothing
        pass

    def _send(self, code: int, body: bytes = b"", content_type: str = "text/plain; charset=utf-8",
              headers: dict | None = None) -> None:
        self.send_response(code)
        for key, value in {"Content-Type": content_type, "Content-Length": str(len(body)),
                           "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                           "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
                           "Content-Security-Policy": PAGE_CSP, **(headers or {})}.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, data: dict) -> None:
        self._send(code, json.dumps(data).encode(), "application/json")

    def _allowed(self) -> bool:
        host = self.headers.get("Host", "")
        if host not in self.server.hosts:
            return False
        if self.command not in ("GET", "HEAD") and self.headers.get("Origin") != f"http://{host}":
            return False
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:  # noqa: BLE001 -- a malformed cookie header is just no cookie
            return False
        sent = cookie.get(COOKIE)
        return sent is not None and hmac.compare_digest(sent.value.encode(), self.server.token.encode())

    def _route(self) -> None:
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        if self.command == "GET" and url.path == "/" and "t" in query:
            # the start address: swap its token for a cookie and drop it from the address bar
            if self.headers.get("Host", "") in self.server.hosts \
                    and hmac.compare_digest(query["t"][0].encode(), self.server.token.encode()):
                return self._send(303, headers={"Location": "/", "Set-Cookie":
                                                f"{COOKIE}={self.server.token}; HttpOnly; SameSite=Strict; Path=/"})
            return self._send(403, b"this address is not this app's; use the one profiler-client app printed")
        if not self._allowed():
            self.close_connection = True
            self._discard_body()
            return self._send(403, b"open the address profiler-client app printed")
        session = self.server.session
        route = (self.command, url.path)
        try:
            if route == ("GET", "/"):
                return self._send(200, files("profiler_client").joinpath("static/app.html").read_bytes(),
                                  "text/html; charset=utf-8")
            if route == ("GET", "/report"):
                if session.report_html is None:
                    return self._send(404, b"no report yet")
                return self._send(200, session.report_html.encode(), "text/html; charset=utf-8")
            if route == ("GET", "/report.json"):
                if session.report_json is None:
                    return self._send(404, b"no report yet")
                return self._send(200, session.report_json, "application/json",
                                  {"Content-Disposition": 'attachment; filename="report.json"'})
            if route == ("PUT", "/api/raw"):
                length = int(self.headers.get("Content-Length") or -1)
                if length < 0:
                    raise ValueError("send the file with its length")
                with session.lock:
                    session.add_raw(query.get("name", [""])[0], length, self.rfile)
                return self._json(200, {"ok": True})
            if self.command == "POST" and url.path.startswith("/api/"):
                body = self._body()
                with session.lock:
                    if url.path == "/api/reset":
                        session.clear_raw()
                        return self._json(200, {"ok": True})
                    if url.path == "/api/prepare":
                        return self._json(200, session.prepare(body.get("keep_names") is True,
                                                               body.get("llm_inputs") is True))
                    if url.path == "/api/submit":
                        return self._json(200, session.submit(str(body.get("token", "")), _prices(body)))
            if route == ("GET", "/api/status"):
                with session.lock:
                    return self._json(200, session.status())
            return self._send(404, b"not found")
        except (ValueError, TypeError, PrepareError, RemoteError, OSError) as exc:
            self.close_connection = True  # a refused upload may leave its body unread
            return self._json(400, {"error": str(exc)})

    def _discard_body(self) -> None:
        """Read and drop a refused request's body, if it is small, so the refusal reaches the sender."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return
        if 0 < length <= MAX_DISCARD_BYTES:
            self.rfile.read(length)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 <= length <= MAX_JSON_BYTES:
            raise ValueError("request too large")
        data = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(data, dict):
            raise TypeError("send a JSON object")
        return data

    do_GET = do_PUT = do_POST = _route


def _prices(body: dict) -> list[float] | None:
    prices = body.get("prices")
    if prices is None:
        return None
    if not (isinstance(prices, list) and len(prices) == 2
            and all(isinstance(p, (int, float)) and not isinstance(p, bool) and 0 <= p <= 1e6 for p in prices)):
        raise ValueError("give both prices in $ per 1M tokens, or neither")
    return [float(p) for p in prices]


def _stop(signum, frame):
    raise KeyboardInterrupt  # so a plain kill also deletes the copied files


# CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT (the last two probably never arrive; see below)
CONSOLE_CLOSE_EVENTS = (2, 5, 6)


def delete_on_console_close(workdir: Path):
    """Windows: delete ``workdir`` when the console window is closed.

    Windows ends the process as soon as the console handlers return for a
    close, before Python's signal handlers and ``finally`` blocks run, so the
    files are deleted in the handler itself. Ctrl-C and Ctrl-Break are passed
    on to Python, whose cleanup in ``serve`` deletes them. Sign-out and
    shutdown are handled the same way if they arrive, but they are probably
    not delivered once the browser has been opened: Windows doesn't send them
    to a console program that has loaded user32.dll (#58). The caller keeps
    the returned handler alive.
    """
    import ctypes
    from ctypes import wintypes

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
    def handler(event):
        if event in CONSOLE_CLOSE_EVENTS:
            shutil.rmtree(workdir, ignore_errors=True)
        return False

    if not ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, True):
        raise ctypes.WinError()
    return handler


def serve(server: str, port: int = 0, open_browser: bool = True) -> None:
    origin = server_origin(server)
    signal.signal(signal.SIGTERM, _stop)
    if hasattr(signal, "SIGBREAK"):  # Windows: Ctrl-Break
        signal.signal(signal.SIGBREAK, _stop)
    workdir = Path(tempfile.mkdtemp(prefix="profiler-client-app-"))  # mode 0700
    _on_close = delete_on_console_close(workdir) if os.name == "nt" else None  # kept alive while serving
    try:
        httpd = AppServer(port, Session(workdir, origin))
        print(f"profiler-client app, sending to {origin}\n\n  {httpd.url}\n\n"
              "Open that address in your browser. Ctrl-C stops the app and deletes the files it copied.", flush=True)
        if open_browser:
            webbrowser.open(httpd.url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            httpd.server_close()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
