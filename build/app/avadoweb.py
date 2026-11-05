"""Small web layer shared by the AVADO Bitcoin packages (Bitcoin node, Electrum
server, Bitcoin explorer). Python standard library only. The same file is in
AVADO-DNP-BitcoinCore, AVADO-DNP-Fulcrum and AVADO-DNP-Mempool; keep the
copies identical.

The page is generic: the package builds a "page model" in plain language
(headline, progress, facts, sections with steps, fields and buttons) and
public/app.js draws it. Package code supplies:

  status()               -> page model (a dict, never secrets)
  actions[name](body)    -> dict; run by the page's buttons (POST /api/action/<name>)
  internal[path](ip)     -> (code, dict); routes for other AVADO apps only

Protections:
- /api/* answers only when the request is addressed to the box (a *.my.ava.do
  name, an IP address or localhost). A site that points its own name at the
  box (DNS rebinding) gets 403.
- Requests that change something (POST) must carry the header X-Avado: 1 and a
  JSON body. A browser only sends a custom header cross-site after a CORS
  preflight, which this server never approves, so another web page cannot
  press the buttons for the user.
- Strict Content-Security-Policy; no inline scripts.

HTTPS: next to port 80 the same page also answers on port 443 with the box's
*.my.ava.do certificate, once there is a usable one (avadotls.py). Without it
the page is http only, as before.
"""

import ipaddress
import json
import os
import socket
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import avadotls

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".json": "application/json",
}

# Only the AVADO Admin (my.ava.do) may show this page inside its own page (its "Setup"
# tab frames the app page); any other site is refused. No X-Frame-Options: it
# cannot name my.ava.do, and "DENY" left the Admin's Setup tab empty.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'self' http://my.ava.do https://my.ava.do; base-uri 'none'; form-action 'none'",
}


def log(*parts):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *parts, flush=True)


def resolve_ips(hostname):
    """All IPv4 addresses Docker's DNS knows for a container name (empty on failure)."""
    try:
        return {info[4][0] for info in socket.getaddrinfo(hostname, None, socket.AF_INET)}
    except OSError:
        return set()


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def write_json(path, data, mode=0o600):
    """Atomic write: a crash never leaves half a file behind."""
    tmp = "%s.tmp-%d-%d" % (path, os.getpid(), threading.get_ident())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def write_text(path, text, mode=0o600):
    """Atomic write of a text file."""
    tmp = "%s.tmp-%d-%d" % (path, os.getpid(), threading.get_ident())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def disk_free(path):
    """(free bytes, total bytes) of the file system holding path."""
    try:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize
    except OSError:
        return None, None


def dir_size(path):
    """Bytes used by a directory tree (allocated blocks, like du)."""
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda _e: None):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_blocks * 512
            except OSError:
                pass
    return total


def human_bytes(value):
    if value is None:
        return "Unknown"
    value = float(value)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if value < 1000 or unit == "TB":
            if unit == "bytes":
                return "%d bytes" % value
            text = ("%.1f" % value).rstrip("0").rstrip(".")
            return "%s %s" % (text, unit)
        value /= 1000.0
    return "%.1f TB" % value


def human_duration(seconds):
    seconds = int(max(0, seconds))
    if seconds < 90:
        return "about a minute"
    minutes = seconds // 60
    if minutes < 90:
        return "about %d minutes" % minutes
    hours = minutes // 60
    if hours < 36:
        return "about %d hours" % hours
    return "about %d days" % round(hours / 24.0)


class Rate:
    """Estimates time left from (time, fraction done) samples over a sliding window."""

    def __init__(self, window=1800):
        self.window = window
        self.samples = []

    def add(self, fraction):
        now = time.time()
        if fraction is None:
            return
        self.samples.append((now, float(fraction)))
        self.samples = [s for s in self.samples if now - s[0] <= self.window]

    def seconds_left(self):
        if len(self.samples) < 2:
            return None
        (t0, f0), (t1, f1) = self.samples[0], self.samples[-1]
        if t1 - t0 < 120 or f1 <= f0:
            return None
        return (1.0 - f1) / ((f1 - f0) / (t1 - t0))


def host_allowed(host_header):
    """True when the request is addressed to the box itself (not a rebinding name)."""
    host = (host_header or "").strip().lower()
    if not host:
        return False
    if host.startswith("["):  # [ipv6]:port
        host = host[1:].split("]", 1)[0]
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    if host in ("localhost", "my.ava.do") or host.endswith(".my.ava.do"):
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    # Docker container and service names of AVADO apps all end in .dappnode.eth.
    # (A bare "dappnodepackage-" prefix is not enough: a rebinding site could
    # use a name such as dappnodepackage-x.example.com.)
    return host.endswith(".dappnode.eth")


class UserError(Exception):
    """An error whose message is plain language and can be shown to the user."""


class WebApp:
    def __init__(self, public_dir, status, actions=None, internal=None, version="", title="AVADO"):
        self.public_dir = os.path.realpath(public_dir)
        self.status = status
        self.actions = actions or {}
        self.internal = internal or {}
        self.version = version
        self.title = title

    def serve(self, port=80, host="0.0.0.0", https_port=None):
        """The page on port; on port 80 also https on 443 (https_port, 0 = off)."""
        app = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "AVADO"
            sys_version = ""
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):  # quiet: no access log spam
                return

            def _send(self, code, body, ctype="application/json", extra=None):
                if isinstance(body, (dict, list)):
                    body = json.dumps(body).encode("utf-8")
                elif isinstance(body, str):
                    body = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for key, value in SECURITY_HEADERS.items():
                    self.send_header(key, value)
                for key, value in (extra or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def do_HEAD(self):
                self.do_GET()

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                try:
                    if path in app.internal:
                        code, body = app.internal[path](self.client_address[0])
                        return self._send(code, body, extra={"Cache-Control": "no-store"})
                    if path.startswith("/api/"):
                        if not host_allowed(self.headers.get("Host")):
                            return self._send(403, {"error": "Forbidden"})
                        if path == "/api/status":
                            model = app.status()
                            model.setdefault("version", app.version)
                            model.setdefault("title", app.title)
                            return self._send(200, model, extra={"Cache-Control": "no-store"})
                        if path == "/api/health":
                            return self._send(200, {"ok": True}, extra={"Cache-Control": "no-store"})
                        return self._send(404, {"error": "Not found"})
                    return self._static(path)
                except Exception:  # never take the server down for one request
                    log("request failed:", path, traceback.format_exc())
                    return self._send(500, {"error": "Something went wrong. Please try again."})

            def do_POST(self):
                path = self.path.split("?", 1)[0]
                try:
                    length = min(int(self.headers.get("Content-Length") or 0), 65536)
                except ValueError:
                    length = 0
                raw = self.rfile.read(length) if length else b""
                if not path.startswith("/api/action/"):
                    return self._send(404, {"error": "Not found"})
                if self.headers.get("X-Avado") != "1" or not host_allowed(self.headers.get("Host")):
                    return self._send(403, {"error": "Forbidden"})
                name = path[len("/api/action/"):]
                handler = app.actions.get(name)
                if handler is None:
                    return self._send(404, {"error": "Unknown action"})
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                    if not isinstance(body, dict):
                        raise ValueError("body must be an object")
                except (ValueError, UnicodeDecodeError):
                    return self._send(400, {"error": "Bad request"})
                try:
                    result = handler(body) or {}
                    return self._send(200, result, extra={"Cache-Control": "no-store"})
                except UserError as err:
                    return self._send(409, {"error": str(err)})
                except Exception:
                    log("action failed:", name, traceback.format_exc())
                    return self._send(500, {"error": "Something went wrong. Please try again."})

            def _static(self, path):
                if path in ("", "/"):
                    path = "/index.html"
                full = os.path.realpath(os.path.join(app.public_dir, path.lstrip("/")))
                if not full.startswith(app.public_dir + os.sep) or not os.path.isfile(full):
                    return self._send(404, "Not found", "text/plain; charset=utf-8")
                ext = os.path.splitext(full)[1]
                with open(full, "rb") as handle:
                    data = handle.read()
                cache = "no-cache" if ext == ".html" else "max-age=300"
                return self._send(200, data, CONTENT_TYPES.get(ext, "application/octet-stream"),
                                  {"Cache-Control": cache})

        server = ThreadingHTTPServer((host, port), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, name="web", daemon=True)
        thread.start()
        log("web page listening on port", port)
        if https_port is None:
            https_port = 443 if port == 80 else 0
        if https_port:
            self.https = avadotls.serve_https(Handler, port=https_port, host=host, log=log)
        return server
