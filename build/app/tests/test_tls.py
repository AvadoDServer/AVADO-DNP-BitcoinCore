"""avadotls: the page also answers on https with the box's *.my.ava.do
certificate, and stays http only (never crashes) without a usable one.
Same file in every package that has avadotls.py.

The certificates below are self-signed test pairs made for these tests
(valid until 2126); they protect nothing.
"""
import http.client
import os
import socket
import ssl
import sys
import tempfile
import time
import unittest
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import avadotls  # noqa: E402

GOOD_CRT = b"""\
-----BEGIN CERTIFICATE-----
MIIBejCCASCgAwIBAgIUAi8JlEFN6pZYIUF9hQ3XjCv8sDkwCgYIKoZIzj0EAwIw
FjEUMBIGA1UEAwwLKi5teS5hdmEuZG8wIBcNMjYxMTA1MTI1MzQ5WhgPMjEyNjEw
MTIxMjUzNDlaMBYxFDASBgNVBAMMCyoubXkuYXZhLmRvMFkwEwYHKoZIzj0CAQYI
KoZIzj0DAQcDQgAEYXPTaSMTySWECRGi0YqT4xqaASeR7ZTnEkP6CA8ypPAew5I6
Ta0oegvRGi3UD0a96Zth6fh1hw/akd43lWuhg6NKMEgwHQYDVR0OBBYEFHNkouNY
fiXN75qJ3pQgVsQ2eDiLMA8GA1UdEwEB/wQFMAMBAf8wFgYDVR0RBA8wDYILKi5t
eS5hdmEuZG8wCgYIKoZIzj0EAwIDSAAwRQIgJmesUpGWeI8GPYxz6AaN6hwAqtL/
C7mOWQWkrx3lfacCIQDMfpqRBttWI94HVRdCX2AHUusaZB5QbXFPV1O13KumOA==
-----END CERTIFICATE-----
"""

GOOD_KEY = b"""\
-----BEGIN PRIVATE KEY-----
MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgrvH0Mmvgd9HsvSix
Cw5EGK+8GIPsZolheWD4O9gAVn6hRANCAARhc9NpIxPJJYQJEaLRipPjGpoBJ5Ht
lOcSQ/oIDzKk8B7DkjpNrSh6C9EaLdQPRr3pm2Hp+HWHD9qR3jeVa6GD
-----END PRIVATE KEY-----
"""

OTHER_CRT = b"""\
-----BEGIN CERTIFICATE-----
MIIBfzCCASagAwIBAgIUXmJBqNfJn9y9HtrSBJFXE7tQ3h4wCgYIKoZIzj0EAwIw
GDEWMBQGA1UEAwwNKi5leGFtcGxlLmNvbTAgFw0yNjExMDUxMjUzNDlaGA8yMTI2
MTAxMjEyNTM0OVowGDEWMBQGA1UEAwwNKi5leGFtcGxlLmNvbTBZMBMGByqGSM49
AgEGCCqGSM49AwEHA0IABFh11s1NvVaoNK+lQRP111LXxsHvrHVxV1xNUVwBuGWU
tj1pVUTYa30RSrzZiEKl6Yk8o7GQyrJHzT1XzO4ehB2jTDBKMB0GA1UdDgQWBBRZ
Pkxotav9b2xh7LUn7XnULbYA0zAPBgNVHRMBAf8EBTADAQH/MBgGA1UdEQQRMA+C
DSouZXhhbXBsZS5jb20wCgYIKoZIzj0EAwIDRwAwRAIgYlO6QQ43mEnVYEAWMr/p
Z/ucYiA7YAh/CCCMMCzyQAoCIFzekMD8DVmcjtmR8Zb+iN7baUm4QizkgXc8w9hu
ufDR
-----END CERTIFICATE-----
"""

OTHER_KEY = b"""\
-----BEGIN PRIVATE KEY-----
MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQglKCizmOFbrR3akAU
EL3VbklpifuXrL1bQwtLVE9kV4ehRANCAARYddbNTb1WqDSvpUET9ddS18bB76x1
cVdcTVFcAbhllLY9aVVE2Gt9EUq82YhCpemJPKOxkMqyR809V8zuHoQd
-----END PRIVATE KEY-----
"""

DAPPMANAGER = "http://dappmanager.my.ava.do"


class Fake:
    """Stands in for http://dappmanager.my.ava.do: {url: bytes or None (down)}."""

    def __init__(self, files=None):
        self.files = dict(files or {})
        self.calls = 0

    def serve(self, cert, key, base=DAPPMANAGER):
        self.files[base + "/my.ava.do.crt"] = cert
        self.files[base + "/my.ava.do.key"] = key

    def __call__(self, url):
        self.calls += 1
        data = self.files.get(url)
        if data is None:
            raise OSError("connection refused")
        return data


class Hello(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        return

    def do_GET(self):
        body = b"same page"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def quiet(*_parts):
    return


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class CheckPairTest(unittest.TestCase):
    def test_good_pair(self):
        context, until = avadotls.check_pair(GOOD_CRT, GOOD_KEY)
        self.assertIsInstance(context, ssl.SSLContext)
        self.assertIn("2126", until)

    def test_full_chain_file_uses_the_first_certificate(self):
        avadotls.check_pair(GOOD_CRT + OTHER_CRT, GOOD_KEY)

    def test_wrong_name(self):
        with self.assertRaisesRegex(avadotls.CertError, "not for"):
            avadotls.check_pair(OTHER_CRT, OTHER_KEY)

    def test_key_of_another_certificate(self):
        with self.assertRaisesRegex(avadotls.CertError, "key does not belong"):
            avadotls.check_pair(GOOD_CRT, OTHER_KEY)

    def test_expired_or_ending_within_the_hour(self):
        end = ssl.cert_time_to_seconds("Oct 12 12:53:49 2126 GMT")
        with self.assertRaisesRegex(avadotls.CertError, "ends"):
            avadotls.check_pair(GOOD_CRT, GOOD_KEY, now=end - 1800)
        avadotls.check_pair(GOOD_CRT, GOOD_KEY, now=end - 7200)

    def test_not_valid_yet(self):
        with self.assertRaisesRegex(avadotls.CertError, "valid only from"):
            avadotls.check_pair(GOOD_CRT, GOOD_KEY, now=0)

    def test_garbage(self):
        for cert, key in ((b"", b""), (b"<html>502</html>", GOOD_KEY), (GOOD_CRT, b"nope"),
                          (b"-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n", GOOD_KEY)):
            with self.assertRaises(avadotls.CertError):
                avadotls.check_pair(cert, key)


class WatcherTest(unittest.TestCase):
    def test_waits_while_the_dappmanager_is_down_then_uses_it(self):
        fake, changes = Fake(), []
        w = avadotls.CertWatcher(lambda w: changes.append(w.valid_until), fetcher=fake, log=quiet)
        self.assertFalse(w.refresh())
        self.assertEqual(changes, [])
        fake.serve(GOOD_CRT, GOOD_KEY)
        self.assertTrue(w.refresh())
        self.assertEqual(len(changes), 1)
        self.assertTrue(w.refresh())  # the same certificate: no change
        self.assertEqual(len(changes), 1)

    def test_second_source(self):
        fake = Fake()
        fake.serve(GOOD_CRT, GOOD_KEY, base="http://iso.ava.do")
        w = avadotls.CertWatcher(lambda w: True, fetcher=fake, log=quiet)
        self.assertTrue(w.refresh())

    def test_bad_new_copy_keeps_the_one_in_use(self):
        fake, changes = Fake(), []
        fake.serve(GOOD_CRT, GOOD_KEY)
        w = avadotls.CertWatcher(lambda w: changes.append(1), fetcher=fake, log=quiet)
        self.assertTrue(w.refresh())
        fake.serve(GOOD_CRT, OTHER_KEY)
        self.assertFalse(w.refresh())
        self.assertEqual(changes, [1])
        self.assertEqual(w.key, GOOD_KEY)

    def test_failing_on_change_is_tried_again(self):
        fake, calls = Fake(), []
        fake.serve(GOOD_CRT, GOOD_KEY)

        def on_change(_w):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return len(calls) > 2  # second call: False (port taken), third: ok

        w = avadotls.CertWatcher(on_change, fetcher=fake, log=quiet)
        self.assertFalse(w.refresh())
        self.assertFalse(w.refresh())
        self.assertTrue(w.refresh())
        self.assertEqual(len(calls), 3)

    def test_says_a_problem_once(self):
        lines = []
        w = avadotls.CertWatcher(lambda w: True, fetcher=Fake(), log=lambda *p: lines.append(p))
        for _ in range(3):
            w.refresh()
        self.assertEqual(len(lines), 1)

    def test_real_fetch_of_a_missing_host_fails_cleanly(self):
        w = avadotls.CertWatcher(lambda w: True, sources=("http://127.0.0.1:1",), log=quiet)
        self.assertFalse(w.refresh())


class ServeHttpsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.ca = os.path.join(tmp, "ca.pem")
        with open(self.ca, "wb") as f:
            f.write(GOOD_CRT)

    def client(self, port):
        # verifies the certificate and the name, as a browser does
        return _NamedHTTPS("lightning.my.ava.do", "127.0.0.1", port, ssl.create_default_context(cafile=self.ca))

    def wait_server(self, watcher):
        for _ in range(100):
            if watcher.server is not None:
                return watcher.server
            time.sleep(0.05)
        self.fail("no https server")

    def test_serves_the_same_handler_over_tls_with_a_valid_certificate(self):
        fake = Fake()
        fake.serve(GOOD_CRT, GOOD_KEY)
        port = free_port()
        w = avadotls.serve_https(Hello, port=port, host="127.0.0.1", log=quiet, fetcher=fake)
        server = self.wait_server(w)
        try:
            conn = self.client(port)
            conn.request("GET", "/")
            res = conn.getresponse()
            self.assertEqual((res.status, res.read()), (200, b"same page"))
            conn.request("GET", "/again")  # keep-alive works
            self.assertEqual(conn.getresponse().read(), b"same page")
            conn.close()
        finally:
            server.shutdown()
            server.server_close()

    def test_no_certificate_no_listener(self):
        port = free_port()
        w = avadotls.serve_https(Hello, port=port, host="127.0.0.1", log=quiet, fetcher=Fake())
        time.sleep(0.3)
        self.assertIsNone(w.server)
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=2).close()

    def test_port_in_use_keeps_http_and_does_not_raise(self):
        busy = socket.socket()
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        fake = Fake()
        fake.serve(GOOD_CRT, GOOD_KEY)
        try:
            lines = []
            w2 = avadotls.serve_https(Hello, port=busy.getsockname()[1], host="127.0.0.1",
                                      log=lambda *p: lines.append(" ".join(map(str, p))), fetcher=fake)
            time.sleep(0.5)
            self.assertIsNone(w2.server)
            self.assertTrue(any("cannot open port" in line for line in lines), lines)
        finally:
            busy.close()

    def test_plain_http_or_a_stalled_client_does_not_block_others(self):
        fake = Fake()
        fake.serve(GOOD_CRT, GOOD_KEY)
        port = free_port()
        w = avadotls.serve_https(Hello, port=port, host="127.0.0.1", log=quiet, fetcher=fake)
        server = self.wait_server(w)
        try:
            stalled = socket.create_connection(("127.0.0.1", port))  # never says hello
            plain = socket.create_connection(("127.0.0.1", port))
            plain.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
            conn = self.client(port)
            conn.request("GET", "/")
            self.assertEqual(conn.getresponse().read(), b"same page")
            conn.close()
            stalled.close()
            plain.close()
        finally:
            server.shutdown()
            server.server_close()

    def test_renewed_certificate_is_swapped_in(self):
        fake = Fake()
        fake.serve(GOOD_CRT, GOOD_KEY)
        port = free_port()
        w = avadotls.serve_https(Hello, port=port, host="127.0.0.1", log=quiet, fetcher=fake)
        server = self.wait_server(w)
        try:
            first = server.context
            fake.serve(GOOD_CRT + b"\n", GOOD_KEY)  # a new copy (same pair, other bytes)
            self.assertTrue(w.refresh())
            self.assertIsNot(server.context, first)
            conn = self.client(port)
            conn.request("GET", "/")
            self.assertEqual(conn.getresponse().read(), b"same page")
            conn.close()
        finally:
            server.shutdown()
            server.server_close()


class NginxTest(unittest.TestCase):
    """The pair and the include file for packages with an nginx front."""

    def setUp(self):
        self.dir = os.path.join(tempfile.mkdtemp(), "https")
        self.fake = Fake()
        self.real_fetch = avadotls.fetch
        avadotls.fetch = self.fake

    def tearDown(self):
        avadotls.fetch = self.real_fetch

    def run_main(self):
        return avadotls.main(["avadotls.py", "nginx", self.dir])

    def test_no_certificate_writes_nothing(self):
        self.assertEqual(self.run_main(), 1)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "https.conf")))

    def test_new_then_unchanged_then_renewed(self):
        self.fake.serve(GOOD_CRT, GOOD_KEY)
        self.assertEqual(self.run_main(), 0)
        with open(os.path.join(self.dir, "https.conf")) as f:
            conf = f.read()
        self.assertIn("listen 443 ssl;", conf)
        self.assertIn("ssl_certificate %s/my.ava.do.crt;" % self.dir, conf)
        self.assertEqual(os.stat(os.path.join(self.dir, "my.ava.do.key")).st_mode & 0o777, 0o600)
        self.assertEqual(self.run_main(), 2)
        self.fake.serve(GOOD_CRT + b"\n", GOOD_KEY)
        self.assertEqual(self.run_main(), 0)
        self.fake.files.clear()  # the DAPPMANAGER is down: keep what is there
        self.assertEqual(self.run_main(), 1)
        self.assertTrue(os.path.getsize(os.path.join(self.dir, "https.conf")) > 0)

    def test_usage(self):
        self.assertEqual(avadotls.main(["avadotls.py"]), 64)


@unittest.skipUnless(os.path.exists(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                 "avadoweb.py")), "no avadoweb.py in this package")
class AvadowebHttpsTest(unittest.TestCase):
    """avadoweb's page answers the same on http and https."""

    def test_same_page_on_both(self):
        import avadoweb

        fake = Fake()
        fake.serve(GOOD_CRT, GOOD_KEY)
        real_fetch, avadotls.fetch = avadotls.fetch, fake
        public = tempfile.mkdtemp()
        with open(os.path.join(public, "index.html"), "w") as f:
            f.write("<p>page</p>")
        try:
            app = avadoweb.WebApp(public, lambda: {"headline": "Hi"}, version="1")
            port = free_port()
            http_server = app.serve(port=0, host="127.0.0.1", https_port=port)
            for _ in range(100):
                if app.https.server is not None:
                    break
                time.sleep(0.05)
            ca = os.path.join(public, "ca.pem")
            with open(ca, "wb") as f:
                f.write(GOOD_CRT)
            plain = http.client.HTTPConnection("127.0.0.1", http_server.server_address[1], timeout=10)
            secure = _NamedHTTPS("bitcoin.my.ava.do", "127.0.0.1", port, ssl.create_default_context(cafile=ca))
            for conn in (plain, secure):
                conn.request("GET", "/")
                self.assertEqual(conn.getresponse().read(), b"<p>page</p>")
                conn.request("GET", "/api/status", headers={"Host": "bitcoin.my.ava.do"})
                res = conn.getresponse()
                self.assertEqual(res.status, 200)
                self.assertIn(b'"headline": "Hi"', res.read())
                conn.close()
            app.https.server.shutdown()
            http_server.shutdown()
        finally:
            avadotls.fetch = real_fetch

    def test_https_only_next_to_port_80(self):
        import avadoweb

        app = avadoweb.WebApp(tempfile.mkdtemp(), dict)
        server = app.serve(port=0, host="127.0.0.1")
        self.assertFalse(hasattr(app, "https"))
        server.shutdown()


class _NamedHTTPS(http.client.HTTPSConnection):
    """Connects to 127.0.0.1 but checks the certificate for a *.my.ava.do name."""

    def __init__(self, name, ip, port, context):
        super().__init__(name, port, context=context, timeout=10)
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


if __name__ == "__main__":
    unittest.main()
