"""HTTP layer of facturi-tools. Standard library only. Bodies, keys and file contents are never logged."""
import hmac
import json
import os
import signal
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from facturi_tools import __version__, inspect_pdf, naming, statement, storage

MAX_BODY = 30 * 1024 * 1024
MIN_KEY = 24
CONN_TIMEOUT = 30
NEEDS_BODY = {"/files", "/inspect", "/statement/parse"}

# route -> needs a request body
ROUTES = {
    ("POST", "/inspect"): True,
    ("POST", "/statement/parse"): True,
    ("POST", "/invoice/plan"): True,
    ("PUT", "/files"): True,
    ("POST", "/match"): True,
    ("PUT", "/admin/rclone-config"): True,
}


class HttpError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _json_body(body):
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise HttpError(400, "body is not valid JSON")
    if not isinstance(data, dict):
        raise HttpError(400, "body must be a JSON object")
    return data


def _write_private(path, data):
    """Atomic write, mode 600. The temp file sits in the same folder so os.replace is atomic."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".rclone-conf-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def rclone_state():
    """Booleans only: never a path or file content."""
    conf = os.environ.get("RCLONE_CONFIG") or ""
    folder = os.path.dirname(os.path.abspath(conf)) if conf else ""
    return {
        "configPresent": bool(conf) and os.path.isfile(conf),
        "configDirWritable": bool(folder) and os.path.isdir(folder) and os.access(folder, os.W_OK | os.X_OK),
        "remoteSet": bool(os.environ.get("FACTURI_REMOTE")),
    }


def buyer_cui_set():
    return any(c.isdigit() for c in os.environ.get("FACTURI_BUYER_CUI") or "")


def startup_warnings():
    st = rclone_state()
    out = []
    if not st["configDirWritable"]:
        out.append("WARNING: the rclone config folder is missing or not writable. Mount a volume or host folder "
                   "(the folder, not the single file) at /config/rclone and make it writable by uid 10001, "
                   "otherwise the config cannot be pushed and OneDrive tokens cannot be refreshed.")
    if not st["configPresent"]:
        out.append("WARNING: no rclone config file yet. Push it with PUT /admin/rclone-config "
                   "(start with FACTURI_ADMIN=1), uploads fail until then.")
    if not st["remoteSet"]:
        out.append("WARNING: FACTURI_REMOTE is not set (for example 'onedrive:Invoices'), "
                   "uploads fail until it is.")
    if not buyer_cui_set():
        out.append("WARNING: FACTURI_BUYER_CUI is not set, so the VAT check on invoices is skipped "
                   "(buyerVatOk is always null).")
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "facturi-tools"
    sys_version = ""
    protocol_version = "HTTP/1.0"  # one request per connection, so an unread body can never be misparsed

    def setup(self):
        # per-connection timeout: a client that never sends its request line or body is dropped
        self.timeout = self.server.conn_timeout
        super().setup()

    # logging: method, path without query, status. Nothing else.
    def log_message(self, fmt, *args):
        path = urlsplit(self.path or "").path
        sys.stderr.write(f"{self.command or '-'} {path} {getattr(self, '_status', '-')}\n")

    def log_error(self, fmt, *args):
        pass

    def send_response(self, code, message=None):
        self._status = code
        super().send_response(code, message)

    def _reply(self, code, payload):
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)

    def _read_body(self):
        raw = (self.headers.get("Content-Length") or "").strip()
        if not (raw.isascii() and raw.isdigit()):
            raise HttpError(400, "Content-Length header is required and must be a number")
        n = int(raw)
        if n > MAX_BODY:
            raise HttpError(413, "body is larger than 30 MB")
        body = self.rfile.read(n)
        if len(body) != n:
            raise HttpError(400, "body is shorter than Content-Length")
        return body

    def _handle(self, method):
        try:
            url = urlsplit(self.path)
            if method == "GET" and url.path == "/health":
                return self._reply(200, {"ok": True, "version": __version__, "buyerCuiSet": buyer_cui_set(),
                                         "rclone": rclone_state()})
            sent = self.headers.get("X-Facturi-Key") or ""
            if not hmac.compare_digest(sent.encode("utf-8"), self.server.key.encode("utf-8")):
                raise HttpError(401, "missing or wrong key")
            route = (method, url.path)
            if route not in ROUTES or (url.path == "/admin/rclone-config" and not self.server.admin):
                raise HttpError(404, "not found")
            body = self._read_body() if ROUTES[route] else b""
            if not body and url.path in NEEDS_BODY:
                raise HttpError(400, "empty body")
            query = parse_qs(url.query, keep_blank_values=True)
            self._reply(200, self._dispatch(url.path, body, query))
        except HttpError as e:
            self._reply(e.code, {"error": e.message})
        except TimeoutError:
            self.close_connection = True  # stalled client: drop it quietly
        except Exception as e:
            # class and handler only: the message can carry request data
            sys.stderr.write(f"unhandled {type(e).__name__} in {self.command} {urlsplit(self.path).path}\n")
            self._reply(500, {"error": "internal error"})

    def _dispatch(self, path, body, query):
        if path == "/inspect":
            name = (query.get("filename") or [None])[0]
            return inspect_pdf.inspect(body, name)
        if path == "/statement/parse":
            return statement.parse_statement(body)
        if path == "/invoice/plan":
            d = _json_body(body)
            if not isinstance(d.get("reader"), dict) or not isinstance(d.get("sha256"), str) \
                    or not isinstance(d.get("original_name"), str):
                raise HttpError(400, "need reader (object), sha256 (string) and original_name (string)")
            return naming.plan_invoice(d["reader"], d["sha256"], d["original_name"])
        if path == "/files":
            raw = (query.get("path") or [None])[0]
            try:
                rel = storage.safe_rel_path(raw)
            except storage.StorageError as e:
                raise HttpError(400, f"bad path: {e}")
            try:
                return storage.upload(rel, body)
            except storage.StorageError as e:
                raise HttpError(500, f"upload failed: {e}")
        if path == "/match":
            d = _json_body(body)
            for k in ("transactions", "invoices", "suppliers"):
                if not isinstance(d.get(k), list):
                    raise HttpError(400, f"{k} must be a list")
            if d.get("statement") is not None and not isinstance(d["statement"], dict):
                raise HttpError(400, "statement must be an object or null")
            try:
                from facturi_tools import matcher
            except ImportError:
                raise HttpError(500, "matcher module is not available in this build")
            return matcher.match(d["transactions"], d["invoices"], d["suppliers"], d.get("statement"))
        if path == "/admin/rclone-config":
            target = os.environ.get("RCLONE_CONFIG")
            if not target:
                raise HttpError(500, "RCLONE_CONFIG is not set")
            _write_private(target, body)
            return {"ok": True}
        raise HttpError(404, "not found")

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")


class FacturiServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, key, admin=False, timeout=CONN_TIMEOUT):
        super().__init__(addr, Handler)
        self.key = key
        self.admin = admin
        self.conn_timeout = timeout


def make_server(host, port, key, admin=False, timeout=CONN_TIMEOUT):
    return FacturiServer((host, port), key, admin, timeout)


def main():
    key = os.environ.get("FACTURI_KEY", "")
    if len(key) < MIN_KEY:
        sys.stderr.write(f"FACTURI_KEY is required and must be at least {MIN_KEY} characters. Exiting.\n")
        raise SystemExit(2)
    port = int(os.environ.get("PORT", "8080"))
    admin = os.environ.get("FACTURI_ADMIN") == "1"
    srv = make_server("0.0.0.0", port, key, admin)
    sys.stderr.write(f"facturi-tools {__version__} listening on port {srv.server_address[1]}"
                     f"{' (admin endpoint ON)' if admin else ''}\n")
    for w in startup_warnings():
        sys.stderr.write(w + "\n")

    def stop(signum, frame):
        # shutdown() blocks until serve_forever returns, so it must not run on the serving thread
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    srv.serve_forever()
    srv.server_close()


if __name__ == "__main__":
    main()
