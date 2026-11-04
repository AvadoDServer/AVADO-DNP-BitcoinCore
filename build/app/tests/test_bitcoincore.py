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


def main_view_words(m):
    """Words an owner sees without opening "Details" (same count as the Electrum Server's test)."""
    parts = [m.get("headline")] + list(m.get("text") or [])
    parts += [(m.get("progress") or {}).get(k) for k in ("left", "right")]
    for f in m.get("facts") or []:
        parts += [f["k"], f["v"]]
    for s in m.get("sections") or []:
        parts += [s.get("title")] + list(s.get("text") or []) + list(s.get("howto") or []) + list(s.get("steps") or [])
        parts += [s.get("after")] if isinstance(s.get("after"), str) else list(s.get("after") or [])
        for f in s.get("fields") or []:
            parts += [f["k"], f["v"]] + (["Copy"] if f.get("copy") else [])
        parts += [b["label"] for b in (s.get("links") or []) + (s.get("buttons") or [])]
        if s.get("more") or s.get("advanced"):
            parts.append(s.get("advancedTitle") or "Details")
    return len(" ".join(p for p in parts if p).split())


def details(m):
    return [s for s in m["sections"] if s.get("id") == "details"][0]


def details_text(m):
    d = details(m)
    return " ".join(" ".join(x.get("text") or []) + " ".join(x.get("steps") or []) for x in d["more"])


class StaleTxindexTest(unittest.TestCase):
    """Seen on the test box: after "Switch to space-saving mode" indexes/txindex stayed."""

    def test_removes_the_old_index_and_keeps_the_rest(self):
        with tempfile.TemporaryDirectory() as d:
            for sub in ("indexes/txindex", "indexes/blockfilter", "blocks", "chainstate"):
                os.makedirs(os.path.join(d, sub))
            open(os.path.join(d, "indexes/txindex/000001.ldb"), "w").close()
            self.assertTrue(main.remove_stale_txindex(d))
            self.assertFalse(os.path.exists(os.path.join(d, "indexes/txindex")))
            for sub in ("indexes/blockfilter", "blocks", "chainstate"):
                self.assertTrue(os.path.isdir(os.path.join(d, sub)), sub)
            self.assertFalse(main.remove_stale_txindex(d))


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


class FreshDownloadTest(unittest.TestCase):
    def test_only_pruned_to_full_starts_again(self):
        self.assertTrue(main.needs_fresh_download("pruned", "full"))
        for old, new in (("full", "pruned"), ("full", "full"), ("pruned", "pruned"), (None, "full"), (None, "pruned")):
            self.assertFalse(main.needs_fresh_download(old, new), (old, new))


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

    def test_first_minutes_say_just_started(self):
        m = self.model({"running": True, "mode": "full", "free": 2000 * GB,
                        "chain": self.chain(blocks=0, headers=0, verificationprogress=0.0, size_on_disk=293),
                        "net": {"connections": 0}})
        adv = details(m)["advanced"]
        self.assertIn({"k": "Blocks checked", "v": "Just started"}, adv)
        self.assertNotIn("Disk used by the node", [f["k"] for f in adv])
        self.assertNotIn("293 bytes", repr(m))
        m = self.model({"running": True, "mode": "full", "free": 2000 * GB, "chain": self.chain(blocks=12),
                        "net": {"connections": 4}})
        adv = details(m)["advanced"]
        self.assertIn({"k": "Blocks checked", "v": "12"}, adv)
        self.assertIn("Disk used by the node", [f["k"] for f in adv])

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
        connect = m["sections"][0]
        self.assertEqual(connect["id"], "connect")  # the Electrum Server and Explorer pages link to #connect
        text = " ".join(connect["text"])
        self.assertIn("need the full Bitcoin history", text)
        self.assertIn("Electrum Server", text)
        self.assertIn("Bitcoin Explorer", text)
        self.assertIn("300 GB free", text)
        self.assertNotIn("buttons", connect)
        self.assertIn("800 GB free when the node was set up", details_text(m))

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
        # the way out is the one card on top
        self.assertEqual(m["sections"][0]["buttons"][0]["action"], "use-pruned")
        self.assertNotIn("buttons", repr(details(m)["more"]))

    def test_main_view_is_short(self):
        """Status, progress and one action on top (about 120 words); the rest behind one "Details"."""
        synced = {"blocks": 920000, "verificationprogress": 0.99999, "initialblockdownload": False}
        cases = [
            ({"mode": "full", "free": 2000 * GB, "chain": self.chain()}, {}),
            ({"mode": "full", "free": 2000 * GB, "chain": self.chain(blocks=0, verificationprogress=0)}, {}),
            ({"mode": "full", "free": 1000 * GB, "chain": self.chain(**synced)}, {}),
            ({"mode": "full", "free": 1000 * GB, "chain": self.chain(**synced)}, {"fulcrum": True, "mempool": True}),
            ({"mode": "pruned", "free": 300 * GB, "chain": self.chain(pruned=True)}, {}),
            ({"mode": "pruned", "free": 1700 * GB, "chain": self.chain(pruned=True, **synced)}, {"mempool": True}),
            ({"mode": "full", "free": 10 * GB, "chain": self.chain()}, {}),
        ]
        for info, installed in cases:
            info = dict(info, running=True, net={"connections": 8, "subversion": "/Satoshi:32.1.0/"},
                        mempool={"size": 5000})
            m = self.model(info, installed=installed)
            self.assertLessEqual(main_view_words(m), 120, (info["mode"], installed))
            self.assertEqual([s["id"] for s in m["sections"]][-1], "details")
            # one "Details", and only on the last card
            self.assertEqual(sum(1 for s in m["sections"] if s.get("more") or s.get("advanced")), 1)
            self.assertLessEqual(len(m["sections"]), 2)
        m = self.model({"running": False, "mode": "full", "free": 1 * GB,
                        "lastExit": {"why": "disk", "at": 0, "pause": 60}})
        self.assertLessEqual(main_view_words(m), 120)

    def test_switch_to_space_saving_is_in_details(self):
        m = self.model({"running": True, "mode": "full", "free": 2000 * GB, "chain": self.chain(),
                        "net": {"connections": 3}})
        disk = [x for x in details(m)["more"] if x["title"] == "Disk space"][0]
        button = disk["buttons"][0]
        self.assertEqual(button["action"], "use-pruned")
        self.assertIn("Bitcoin Explorer stop working", button["confirm"])
        self.assertNotIn("buttons", m["sections"][0])
        # MODE=full in the Admin: no button
        m = self.model({"running": True, "mode": "full", "free": 2000 * GB, "chain": self.chain(),
                        "net": {"connections": 3}}, MODE="full")
        self.assertNotIn("buttons", repr(details(m)["more"]))

    def test_space_saving_never_promises_the_explorer(self):
        m = self.model({"running": True, "mode": "pruned", "free": 300 * GB, "chain": self.chain(pruned=True),
                        "net": {"connections": 8}}, installed={"mempool": True})
        explorer = [x for x in details(m)["more"] if x["title"] == "Bitcoin Explorer"][0]
        self.assertIn("needs the full node", " ".join(explorer["text"]))
        self.assertNotIn("links", explorer)
        self.assertNotIn("mempool.my.ava.do", repr(m))

    def test_wallet_steps_in_details(self):
        m = self.model({"running": True, "mode": "full", "free": 2000 * GB, "chain": self.chain(),
                        "net": {"connections": 3}})
        self.assertNotIn("steps", m["sections"][0])
        self.assertIn("Install \"Electrum Server\"", details_text(m))

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
        for host in ("evil.example", "my.ava.do.evil.example", "dappnodepackage-x.evil.example",
                     "dappnodepackage-bitcoincore.avado.dnp.dappnode.eth.evil.example", "", None):
            self.assertFalse(avadoweb.host_allowed(host), host)


class TimeLeftTest(unittest.TestCase):
    def test_no_wild_estimate_at_the_start(self):
        # Seen on the test box: 0.0% done gave "about 89 days left".
        self.assertEqual(main.time_left_text(0.0004, 89 * 86400), "Usually 1 to 3 days in total")

    def test_no_estimate_yet(self):
        self.assertEqual(main.time_left_text(0.5, None), "Usually 1 to 3 days in total")

    def test_estimate_after_ten_percent(self):
        self.assertIn("left", main.time_left_text(0.4, 20 * 3600))
        self.assertNotIn("days in total", main.time_left_text(0.4, 20 * 3600))

    def test_very_slow_node(self):
        self.assertEqual(main.time_left_text(0.4, 40 * 86400), "More than 2 weeks left at this speed")


class FramingTest(unittest.TestCase):
    """The Admin's Setup tab shows this page in a frame; other sites may not."""

    def test_only_the_admin_may_frame_the_page(self):
        import avadoweb
        csp = avadoweb.SECURITY_HEADERS["Content-Security-Policy"]
        self.assertIn("frame-ancestors 'self' http://my.ava.do https://my.ava.do;", csp)
        self.assertNotIn("X-Frame-Options", avadoweb.SECURITY_HEADERS)


if __name__ == "__main__":
    unittest.main()
