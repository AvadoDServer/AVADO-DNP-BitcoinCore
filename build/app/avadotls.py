"""HTTPS for an AVADO app page, with the box's *.my.ava.do certificate.
Python standard library only.

Chrome's "Always use secure connections" (on by default) tries
https://<app>.my.ava.do first when an owner types the http address. A page
that does not answer on port 443 then shows "This site doesn't support a
secure connection". So the page also answers on 443, with the same
certificate as the Admin (https://admin.my.ava.do), Vaultwarden and the other
AVADO apps with https: the box's DAPPMANAGER serves its current copy at
http://dappmanager.my.ava.do (iso.ava.do is the second source).

- The certificate is used only when it is for *.my.ava.do, valid for at least
  another hour and matches its key. Until there is one (no DAPPMANAGER yet,
  no network), the page stays http only, and it is checked again every
  5 minutes; after that once a day. Nothing here ever stops the page.
- The TLS handshake runs in each connection's own thread, so a slow or broken
  client never holds up the others.
- http keeps working exactly as before (also inside the Admin's Setup tab).

The same file is in AVADO-DNP-BitcoinCore, -Fulcrum, -Mempool, -Lightning,
-MoneroNode and -Private-Assistant; keep the copies identical.

  serve_https(handler_class)       the page's own handler, also on port 443
  CertWatcher(on_change).start()   just the certificate (for an nginx front:
                                   save_pair() and nginx_conf())
  python3 avadotls.py nginx DIR    the same once, for a start script (exit
                                   0: new files in DIR, reload nginx; 2: no
                                   change; 1: no usable certificate)
"""

import os
import ssl
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

SOURCES = ("http://dappmanager.my.ava.do", "http://iso.ava.do")
NAME = "*.my.ava.do"
RETRY_SECONDS = 300
REFRESH_SECONDS = 86400
MIN_LEFT_SECONDS = 3600
HANDSHAKE_SECONDS = 15
IDLE_SECONDS = 300
MAX_BYTES = 64 * 1024


def _log(*parts):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *parts, flush=True)


class CertError(Exception):
    """The certificate cannot be used (the message says why)."""


def fetch(url, timeout=20):
    """GET url (no proxy), at most MAX_BYTES."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as res:
        data = res.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise CertError("%s is too large" % url)
    return data


def check_pair(cert, key, now=None):
    """(ssl context, "valid until" text) for a usable pair, else CertError."""
    if b"-----BEGIN CERTIFICATE-----" not in cert:
        raise CertError("no certificate in the file")
    if b"PRIVATE KEY-----" not in key:
        raise CertError("no key in the file")
    decode = getattr(getattr(ssl, "_ssl", None), "_test_decode_cert", None)
    if decode is None:
        raise CertError("this Python cannot read certificates")
    with tempfile.TemporaryDirectory(prefix="avado-tls-") as tmp:
        crt_path, key_path = os.path.join(tmp, "crt"), os.path.join(tmp, "key")
        for path, data in ((crt_path, cert), (key_path, key)):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
        try:
            info = decode(crt_path)  # the first certificate in the file
        except (ssl.SSLError, ValueError, OSError) as err:
            raise CertError("unreadable certificate (%s)" % err)
        names = [value for kind, value in info.get("subjectAltName", ()) if kind == "DNS"]
        if NAME not in names:
            raise CertError("the certificate is not for %s (%s)" % (NAME, ", ".join(names) or "no names"))
        now = time.time() if now is None else now
        try:
            not_before = ssl.cert_time_to_seconds(info["notBefore"])
            not_after = ssl.cert_time_to_seconds(info["notAfter"])
        except (KeyError, ValueError):
            raise CertError("the certificate has no valid dates")
        if not_before > now:
            raise CertError("the certificate is valid only from %s" % info["notBefore"])
        if not_after - now < MIN_LEFT_SECONDS:
            raise CertError("the certificate ends %s" % info["notAfter"])
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        # browsers often just close the connection: not an error worth a log line
        context.options |= getattr(ssl, "OP_IGNORE_UNEXPECTED_EOF", 0)
        try:
            context.load_cert_chain(crt_path, key_path)
        except (ssl.SSLError, OSError) as err:
            raise CertError("the key does not belong to the certificate (%s)" % err)
    return context, info["notAfter"]


class CertWatcher:
    """Keeps the box's *.my.ava.do certificate: every 5 minutes until there is
    one, then daily. on_change(watcher) runs when a new one is in place; it may
    return False (for example: the port is taken) to be called again later."""

    def __init__(self, on_change, sources=SOURCES, fetcher=None, log=_log):
        self.on_change = on_change
        self.sources = sources
        self.fetcher = fetcher or fetch
        self.log = log
        self.cert = self.key = self.context = self.valid_until = None
        self.server = None  # set by serve_https
        self._said = None

    def refresh(self):
        """True when a usable certificate is in place (new or the same)."""
        problems = []
        for base in self.sources:
            try:
                cert = self.fetcher(base + "/my.ava.do.crt")
                key = self.fetcher(base + "/my.ava.do.key")
                context, until = check_pair(cert, key)
            except Exception as err:  # never take the page down for this
                problems.append("%s: %s" % (base, err))
                continue
            self._said = None
            if (cert, key) == (self.cert, self.key):
                return True
            self.cert, self.key, self.context, self.valid_until = cert, key, context, until
            try:
                applied = self.on_change(self) is not False
            except Exception as err:
                self.log("HTTPS: could not use the new certificate:", err)
                applied = False
            if not applied:
                self.cert = self.key = None  # try again next round
            return applied
        text = "; ".join(problems)
        if text != self._said:  # say it once, not every 5 minutes
            self.log("HTTPS: no usable *.my.ava.do certificate yet, http only for now (%s)" % text)
            self._said = text
        return False

    def start(self):
        def loop():
            while True:
                try:
                    ok = self.refresh()
                except Exception as err:
                    self.log("HTTPS: certificate check failed:", err)
                    ok = False
                time.sleep(REFRESH_SECONDS if ok else RETRY_SECONDS)

        threading.Thread(target=loop, name="https-cert", daemon=True).start()
        return self


class TLSHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with TLS; the context can be swapped at any time."""

    daemon_threads = True

    def __init__(self, address, handler_class, context):
        self.context = context
        super().__init__(address, handler_class)

    def finish_request(self, request, client_address):
        request.settimeout(HANDSHAKE_SECONDS)
        try:
            conn = self.context.wrap_socket(request, server_side=True)
        except OSError:  # plain http on 443, a scanner, a closed tab: just drop it
            return
        conn.settimeout(IDLE_SECONDS)
        try:
            self.RequestHandlerClass(conn, client_address, self)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], OSError):  # a dropped connection, not a bug
            return
        super().handle_error(request, client_address)


def serve_https(handler_class, port=443, host="0.0.0.0", log=_log, sources=SOURCES, fetcher=None):
    """Serves handler_class on port with TLS once the certificate is there.
    Returns the CertWatcher (its .server is the HTTPS server, or None)."""

    def on_change(watcher):
        if watcher.server is not None:
            watcher.server.context = watcher.context
            log("HTTPS: certificate renewed, valid until", watcher.valid_until)
            return True
        try:
            server = TLSHTTPServer((host, port), handler_class, watcher.context)
        except OSError as err:
            log("HTTPS: cannot open port %s (%s); the page works over http" % (port, err))
            return False
        threading.Thread(target=server.serve_forever, name="web-https", daemon=True).start()
        watcher.server = server
        log("secure web page listening on port", server.server_address[1],
            "(certificate valid until %s)" % watcher.valid_until)
        return True

    return CertWatcher(on_change, sources=sources, fetcher=fetcher, log=log).start()


def save_pair(cert, key, directory):
    """Writes my.ava.do.crt and my.ava.do.key (only the owner can read the key)."""
    os.makedirs(directory, mode=0o700, exist_ok=True)
    for name, data, mode in (("my.ava.do.crt", cert, 0o644), ("my.ava.do.key", key, 0o600)):
        path = os.path.join(directory, name)
        tmp = "%s.tmp-%d" % (path, os.getpid())
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)


def nginx_conf(directory, listen="listen 443 ssl;"):
    """nginx lines for a server block: https with the pair save_pair() wrote."""
    return ("%s\nssl_certificate %s/my.ava.do.crt;\nssl_certificate_key %s/my.ava.do.key;\n"
            "ssl_protocols TLSv1.2 TLSv1.3;\n" % (listen, directory, directory))


def _read(path):
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return None


def main(argv):
    """python3 avadotls.py nginx DIR: DIR/https.conf (include it in the server
    block; empty means http only) and the pair, when the box has a new one."""
    if len(argv) != 3 or argv[1] != "nginx":
        print("usage: avadotls.py nginx DIR", file=sys.stderr)
        return 64
    directory = os.path.abspath(argv[2])
    conf = os.path.join(directory, "https.conf")
    have = (_read(os.path.join(directory, "my.ava.do.crt")), _read(os.path.join(directory, "my.ava.do.key")))
    watcher = CertWatcher(lambda w: True)
    if not watcher.refresh():
        return 1
    if (watcher.cert, watcher.key) == have and _read(conf):
        return 2
    save_pair(watcher.cert, watcher.key, directory)
    with open(conf + ".tmp", "w", encoding="utf-8") as handle:
        handle.write(nginx_conf(directory))
    os.replace(conf + ".tmp", conf)
    _log("HTTPS: certificate valid until %s saved for nginx" % watcher.valid_until)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
