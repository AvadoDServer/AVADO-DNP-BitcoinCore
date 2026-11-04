import hashlib
import hmac
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import avadoweb  # noqa: E402
import main  # noqa: E402

GB = 1000 ** 3


def config(**env):
    return main.Config(env)


class DecideModeTest(unittest.TestCase):
    def test_auto_full_when_enough_space(self):
        self.assertEqual(main.decide_mode(config(), {}, 2000 * GB), ("full", "auto"))

    def test_auto_pruned_when_short_of_space(self):
        self.assertEqual(main.decide_mode(config(), {}, 1499 * GB), ("pruned", "auto"))

    def test_auto_pruned_when_space_unknown(self):
        self.assertEqual(main.decide_mode(config(), {}, None), ("pruned", "auto"))

    def test_remembered_choice_wins_over_free_space(self):
        self.assertEqual(main.decide_mode(config(), {"mode": "pruned", "reason": "auto"}, 3000 * GB), ("pruned", "auto"))
        self.assertEqual(main.decide_mode(config(), {"mode": "full", "reason": "owner"}, 10 * GB), ("full", "owner"))

    def test_setting_wins_over_everything(self):
        self.assertEqual(main.decide_mode(config(MODE="pruned"), {"mode": "full"}, 3000 * GB), ("pruned", "setting"))
        self.assertEqual(main.decide_mode(config(MODE="FULL"), {}, 10 * GB), ("full", "setting"))

    def test_bad_mode_falls_back_to_auto(self):
        self.assertEqual(config(MODE="archive").mode, "auto")

    def test_threshold_setting(self):
        self.assertEqual(main.decide_mode(config(FULL_NODE_MIN_FREE_GB="900"), {}, 1000 * GB), ("full", "auto"))


class ConfTest(unittest.TestCase):
    def setUp(self):
        self.logins = {app: {"user": app, "password": "pw-" + app, "salt": "00" * 16} for app in main.APPS}

    def test_rpcauth_matches_bitcoin_core_format(self):
        line = main.rpcauth_line("fulcrum", "secret", salt="abcd")
        digest = hmac.new(b"abcd", b"secret", hashlib.sha256).hexdigest()
        self.assertEqual(line, "rpcauth=fulcrum:abcd$" + digest)

    def test_no_password_in_conf(self):
        conf = main.render_conf(config(), "full", self.logins)
        for app in main.APPS:
            self.assertNotIn("pw-" + app, conf)
            self.assertIn("rpcauth=%s:" % app, conf)
            self.assertIn("rpcwhitelist=%s:" % app, conf)
        self.assertIn("prune=0", conf)
        self.assertIn("txindex=1", conf)
        self.assertIn("disablewallet=1", conf)
        self.assertIn("rpcallowip=172.33.0.0/16", conf)
        self.assertNotIn("rpcpassword", conf)
        # No log file inside the volume (it would grow without limit); the container log is enough.
        self.assertIn("nodebuglogfile=1", conf)
        self.assertNotIn("debuglogfile=0", conf)
        self.assertIn("listenonion=0", conf)

    def test_pruned_conf(self):
        conf = main.render_conf(config(PRUNE_GB="10"), "pruned", self.logins)
        self.assertIn("prune=9536", conf)
        self.assertIn("txindex=0", conf)

    def test_prune_never_below_bitcoin_core_minimum(self):
        self.assertEqual(main.prune_mib(0), 550)

    def test_whitelists_have_no_dangerous_methods(self):
        dangerous = {"stop", "setban", "addnode", "disconnectnode", "invalidateblock", "pruneblockchain",
                     "setnetworkactive", "dumptxoutset", "loadtxoutset", "savemempool", "importmempool",
                     "submitblock", "generatetoaddress", "logging"}
        for app, spec in main.APPS.items():
            self.assertFalse(dangerous & set(spec["methods"]), app)

    def test_page_login_is_read_only(self):
        self.assertNotIn("sendrawtransaction", main.APPS["avado"]["methods"])

    def test_extra_opts_only_options(self):
        self.assertEqual(config(EXTRA_OPTS="-maxconnections=20 rm -datadir=/x -rpcpassword=a").extra_opts,
                         ["-maxconnections=20"])

    def test_network_specific_options_on_command_line(self):
        args = main.bitcoind_args(config(NETWORK="regtest"))
        self.assertIn("-chain=regtest", args)
        self.assertIn("-rpcport=18443", args)


class LoginsTest(unittest.TestCase):
    def test_logins_made_once_and_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "logins.json")
            first = main.load_logins(path)
            self.assertEqual(set(first), set(main.APPS))
            self.assertGreaterEqual(len(first["fulcrum"]["password"]), 40)
            self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
            self.assertEqual(main.load_logins(path), first)


class ExitTest(unittest.TestCase):
    def test_disk_full(self):
        self.assertEqual(main.explain_exit(["Error: Disk space is too low!"]), "disk")

    def test_database(self):
        self.assertEqual(main.explain_exit(["Error: Corrupted block database detected."]), "database")

    def test_unknown(self):
        self.assertIsNone(main.explain_exit(["Shutdown: done"]))


class ModelTest(unittest.TestCase):
    def model(self, info, state=None, installed=None, **env):
        return main.build_model(info, config(**env), state or {"reason": "auto", "freeAtDecision": 800 * GB},
                                avadoweb.Rate(), None, installed or {})

    def chain(self, **kw):
        base = {"blocks": 500000, "headers": 920000, "verificationprogress": 0.42, "initialblockdownload": True,
                "size_on_disk": 300 * GB, "pruned": False}
        base.update(kw)
        return base

    def test_downloading(self):
        m = self.model({"running": True, "mode": "full", "free": 2000 * GB, "chain": self.chain(),
                        "net": {"connections": 10}})
        self.assertEqual(m["state"], "working")
        self.assertIn("downloading", m["headline"])
        self.assertEqual(m["progress"]["percent"], 42.0)
        self.assertIn("920,000", m["progress"]["left"])

    def test_headers_first(self):
        m = self.model({"running": True, "mode": "pruned", "free": 2000 * GB,
                        "chain": self.chain(blocks=0, headers=632600, verificationprogress=0.0),
                        "net": {"connections": 1}})
        self.assertIn("list of all blocks", m["progress"]["left"])
        self.assertIn("632,600", m["progress"]["left"])
        self.assertIn({"k": "Connected to", "v": "1 Bitcoin node", "tone": None}, m["facts"])

    def test_synced_full_without_fulcrum_offers_store(self):
        m = self.model({"running": True, "mode": "full", "free": 1000 * GB,
                        "chain": self.chain(blocks=920000, verificationprogress=0.99999, initialblockdownload=False),
                        "net": {"connections": 10}})
        self.assertEqual(m["state"], "ok")
        connect = m["sections"][0]
        self.assertEqual(connect["id"], "connect")
        self.assertIn("Electrum Server", " ".join(connect["text"]))
        self.assertEqual(connect["links"][0]["href"], "http://my.ava.do/#/installer")

    def test_full_with_fulcrum_links_to_it(self):
        m = self.model({"running": True, "mode": "full", "free": 1000 * GB, "chain": self.chain(),
                        "net": {"connections": 3}}, installed={"fulcrum": True})
        self.assertEqual(m["sections"][0]["links"][0]["href"], "http://fulcrum.my.ava.do")

    def test_pruned_says_why_electrum_cannot_run(self):
        m = self.model({"running": True, "mode": "pruned", "free": 300 * GB, "chain": self.chain(pruned=True),
                        "net": {"connections": 8}})
        text = " ".join(m["sections"][0]["text"])
        self.assertIn("needs the full Bitcoin history", text)
        self.assertIn("800 GB free", text)
        self.assertNotIn("buttons", m["sections"][0])

    def test_pruned_offers_full_when_space_now_fits(self):
        m = self.model({"running": True, "mode": "pruned", "free": 1700 * GB, "chain": self.chain(pruned=True),
                        "net": {"connections": 8}})
        self.assertEqual(m["sections"][0]["buttons"][0]["action"], "use-full")
        m = self.model({"running": True, "mode": "pruned", "free": 900 * GB, "chain": self.chain(pruned=True),
                        "net": {"connections": 8}})
        self.assertNotIn("buttons", m["sections"][0])

    def test_stopped_disk_full(self):
        m = self.model({"running": False, "mode": "full", "free": 1 * GB,
                        "lastExit": {"why": "disk", "at": 0, "pause": 60}})
        self.assertEqual(m["state"], "error")
        self.assertIn("disk is full", m["headline"])

    def test_warmup(self):
        m = self.model({"running": True, "mode": "full", "free": 1000 * GB, "warmup": "Loading block index…"})
        self.assertIn("loading its list of blocks", m["text"][0])

    def test_no_secrets_in_model(self):
        m = self.model({"running": True, "mode": "full", "free": 1000 * GB, "chain": self.chain(),
                        "net": {"connections": 1, "subversion": "/Satoshi:32.1.0/"}})
        flat = repr(m).lower()
        self.assertNotIn("password", flat)
        self.assertNotIn("rpcauth", flat)


class HostTest(unittest.TestCase):
    def test_allowed(self):
        for host in ("bitcoincore.my.ava.do", "my.ava.do:80", "172.33.0.5", "localhost:8080", "[::1]:80",
                     "DAppNodePackage-bitcoincore.avado.dnp.dappnode.eth"):
            self.assertTrue(avadoweb.host_allowed(host), host)

    def test_refused(self):
        for host in ("evil.example", "my.ava.do.evil.example", "", None):
            self.assertFalse(avadoweb.host_allowed(host), host)


if __name__ == "__main__":
    unittest.main()
