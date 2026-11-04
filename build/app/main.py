#!/usr/bin/env python3
"""AVADO Bitcoin Node: runs Bitcoin Core (bitcoind) and its status page.

- Chooses a full or a space-saving (pruned) node from the free disk space the
  first time it starts (MODE=auto), and remembers the choice.
- Makes one RPC login per app (this page, the Electrum server, the explorer)
  with a random password, stored only in this app's volume. bitcoin.conf holds
  salted hashes (rpcauth), never the passwords. Each login may call only the
  RPC methods that app needs (rpcwhitelist).
- Gives an app its login only when the request comes from that app's own
  container (GET /internal/rpc-login/<app>).
- Restarts bitcoind when it stops, and explains on the page why it stopped.
"""

import collections
import hashlib
import hmac
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time

import avadoweb
from avadoweb import UserError, log
from bitcoinrpc import RPC_IN_WARMUP, BitcoinRpc, RpcError

VERSION = (open(os.path.join(os.path.dirname(__file__), "package-version")).read().strip()
           if os.path.exists(os.path.join(os.path.dirname(__file__), "package-version")) else "dev")

DATA_DIR = os.environ.get("DATA_DIR", "/data")
BTC_DIR = os.path.join(DATA_DIR, "bitcoin")
STATE_DIR = os.path.join(DATA_DIR, "avado")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
LOGINS_FILE = os.path.join(STATE_DIR, "rpc-logins.json")
CONF_FILE = os.path.join(BTC_DIR, "bitcoin.conf")
PUBLIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public")

GB = 1000 ** 3
NETWORKS = {"mainnet": "main", "testnet4": "testnet4", "signet": "signet", "regtest": "regtest"}
RPC_PORTS = {"main": 8332, "testnet4": 48332, "signet": 38332, "regtest": 18443}
P2P_PORTS = {"main": 8333, "testnet4": 48333, "signet": 38333, "regtest": 18444}

# What the full node needs on disk (Nov 2026: blocks ~770 GB, transaction index
# ~60 GB, chain state ~12 GB). With the Electrum server (~200 GB) about 1.05 TB;
# FULL_NODE_MIN_FREE_GB (1.5 TB) leaves room to grow for a few years. Below
# that the node starts in space-saving mode.
FULL_HISTORY_GB = 850

# What each app may ask Bitcoin Core. Taken from the apps' source code
# (Fulcrum 2.1.4 src/BitcoinD.cpp, Controller, Servers; mempool 3.3.2
# backend rpc-api calls) and checked against a test run with -debug=rpc.
COMMON_READ = [
    "getbestblockhash", "getblock", "getblockchaininfo", "getblockcount", "getblockhash",
    "getblockheader", "getblockstats", "getchaintips", "getdifficulty", "getindexinfo",
    "getmempoolinfo", "getmempoolentry", "getmempoolancestors", "getmempooldescendants",
    "getnetworkinfo", "getrawmempool", "getrawtransaction", "gettxout", "uptime", "help",
    "estimatesmartfee", "validateaddress", "decoderawtransaction", "testmempoolaccept",
]
BROADCAST = ["sendrawtransaction", "submitpackage"]
APPS = {
    # name -> container names allowed to fetch the login, RPC methods
    "avado": {"hosts": [], "methods": [
        "getblockchaininfo", "getnetworkinfo", "getmempoolinfo", "getindexinfo", "uptime",
        "getblockcount", "getbestblockhash", "help"]},
    "fulcrum": {"hosts": ["DAppNodePackage-fulcrum.avado.dnp.dappnode.eth", "fulcrum.avado.dnp.dappnode.eth"],
                "methods": COMMON_READ + BROADCAST + ["getzmqnotifications", "estimatefee"]},
    "mempool": {"hosts": ["DAppNodePackage-mempool.avado.dnp.dappnode.eth", "mempool.avado.dnp.dappnode.eth"],
                "methods": COMMON_READ + BROADCAST + [
                    "getnetworkhashps", "gettxoutsetinfo", "getmininginfo", "getchaintxstats", "ping",
                    "getconnectioncount", "getblockfilter"]},
}


def env_int(name, default, low=None, high=None, env=os.environ):
    try:
        value = int(str(env.get(name, default)).strip())
    except ValueError:
        value = default
    if low is not None:
        value = max(low, value)
    if high is not None:
        value = min(high, value)
    return value


class Config:
    def __init__(self, env=os.environ):
        self.mode = (env.get("MODE") or "auto").strip().lower()
        if self.mode not in ("auto", "full", "pruned"):
            log("MODE=%r is not auto, full or pruned; using auto" % self.mode)
            self.mode = "auto"
        self.full_min_free_gb = env_int("FULL_NODE_MIN_FREE_GB", 1500, 100, env=env)
        self.prune_gb = env_int("PRUNE_GB", 10, 1, 10000, env=env)
        # 1000 MB: fast enough on an AVADO, and small enough to be written out within
        # the 10 seconds Docker waits when the app is stopped.
        self.dbcache_mb = env_int("DBCACHE_MB", 1000, 100, 32000, env=env)
        network = (env.get("NETWORK") or "mainnet").strip().lower()
        self.chain = NETWORKS.get(network, "main")
        self.rpc_allow = [s.strip() for s in (env.get("RPC_ALLOW") or "172.33.0.0/16").split(",") if s.strip()]
        # Extra bitcoind options for support (passed as arguments, never through a shell).
        # The data folder, the config file and logins stay under the app's control.
        self.extra_opts = [o for o in (env.get("EXTRA_OPTS") or "").split()
                           if re.fullmatch(r"-[a-z][a-z0-9]*(=\S*)?", o)
                           and o.split("=", 1)[0] not in ("-datadir", "-conf", "-includeconf", "-rpcauth",
                                                           "-rpcuser", "-rpcpassword", "-rpcwhitelist")]
        self.bitcoind = env.get("BITCOIND", "/usr/local/bin/bitcoind")
        # Extra container names per app, for tests outside AVADO only.
        self.extra_hosts = {}
        for app in APPS:
            extra = env.get("APP_HOSTS_%s" % app.upper())
            if extra:
                self.extra_hosts[app] = [h.strip() for h in extra.split(",") if h.strip()]

    @property
    def rpc_port(self):
        return RPC_PORTS[self.chain]

    @property
    def p2p_port(self):
        return P2P_PORTS[self.chain]


# ---------------------------------------------------------------- decisions

def decide_mode(config, state, free_bytes):
    """Returns (mode, reason). The owner's choice on the page wins over auto;
    MODE=full/pruned set in the Admin wins over both."""
    if config.mode in ("full", "pruned"):
        return config.mode, "setting"
    if state.get("mode") in ("full", "pruned"):
        return state["mode"], state.get("reason") or "auto"
    if free_bytes is not None and free_bytes >= config.full_min_free_gb * GB:
        return "full", "auto"
    return "pruned", "auto"


def needs_fresh_download(old_mode, new_mode):
    """A pruned node has thrown old blocks away; Bitcoin Core cannot become a full
    node from it (it stops with "rebuild the database using -reindex"), so the
    download starts again from nothing."""
    return old_mode == "pruned" and new_mode == "full"


def rpcauth_line(user, password, salt=None):
    """bitcoin.conf rpcauth entry, as share/rpcauth/rpcauth.py makes it."""
    salt = salt or secrets.token_hex(16)
    digest = hmac.new(salt.encode("utf-8"), password.encode("utf-8"), hashlib.sha256).hexdigest()
    return "rpcauth=%s:%s$%s" % (user, salt, digest)


def load_logins(path=LOGINS_FILE):
    logins = avadoweb.read_json(path, {}) or {}
    changed = False
    for app in APPS:
        entry = logins.get(app)
        if not isinstance(entry, dict) or not entry.get("password") or not entry.get("salt"):
            logins[app] = {"user": app, "password": secrets.token_urlsafe(32), "salt": secrets.token_hex(16)}
            changed = True
    if changed:
        avadoweb.write_json(path, logins)
    return logins


def prune_mib(gb):
    return max(550, int(gb * GB / (1024 * 1024)))


def render_conf(config, mode, logins):
    lines = [
        "# Written by the AVADO Bitcoin Node app at every start: changes here are lost.",
        "# To change a setting, use the app's page or its settings in the AVADO Admin.",
        "server=1",
        "disablewallet=1",
        # The Electrum server (Fulcrum) needs the transaction index; a pruned node cannot have one.
        "txindex=%d" % (1 if mode == "full" else 0),
        "printtoconsole=1",
        # Log to the container only. ("debuglogfile=0" would write a log file named "0".)
        "nodebuglogfile=1",
        "logtimestamps=1",
        "dbcache=%d" % config.dbcache_mb,
        "maxmempool=300",
        "prune=%d" % (prune_mib(config.prune_gb) if mode == "pruned" else 0),
        "listen=1",
        "natpmp=0",
        # No Tor on AVADO: do not look for a Tor control port (log noise every few minutes).
        "listenonion=0",
        "rpcallowip=127.0.0.1",
    ]
    lines += ["rpcallowip=%s" % net for net in config.rpc_allow]
    lines += [
        # New blocks and transactions for the Electrum server, inside the AVADO only
        "zmqpubhashblock=tcp://0.0.0.0:28332",
        "zmqpubrawblock=tcp://0.0.0.0:28332",
        "zmqpubrawtx=tcp://0.0.0.0:28333",
        # Users without a whitelist (only the cookie inside this container) keep every method.
        "rpcwhitelistdefault=0",
    ]
    for app, spec in APPS.items():
        entry = logins[app]
        lines.append(rpcauth_line(entry["user"], entry["password"], entry["salt"]))
        lines.append("rpcwhitelist=%s:%s" % (entry["user"], ",".join(sorted(set(spec["methods"])))))
    return "\n".join(lines) + "\n"


def bitcoind_args(config):
    # Network-specific options go on the command line so they apply to whichever
    # network runs (in bitcoin.conf they would need a [section]).
    return [
        config.bitcoind,
        "-datadir=%s" % BTC_DIR,
        "-conf=%s" % CONF_FILE,
        "-chain=%s" % config.chain,
        "-rpcbind=0.0.0.0",
        "-rpcport=%d" % config.rpc_port,
        "-port=%d" % config.p2p_port,
    ] + config.extra_opts


# ---------------------------------------------------------------- bitcoind

class Bitcoind:
    """Keeps bitcoind running; remembers why it stopped."""

    def __init__(self, config):
        self.config = config
        self.lock = threading.RLock()
        self.proc = None
        self.wanted = True
        self.lines = collections.deque(maxlen=80)
        self.started_at = None
        self.last_exit = None  # {"code", "at", "why"}
        self.restarts = 0
        self.next_start = 0
        self.prepare = None  # callable run before every start (writes bitcoin.conf)

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def _reader(self, proc):
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            self.lines.append(line)
            print(line, flush=True)

    def start(self):
        with self.lock:
            if self.running() or not self.wanted:
                return
            if self.prepare:
                self.prepare()
            args = bitcoind_args(self.config)
            log("starting", " ".join(args))
            self.lines.clear()
            self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            self.started_at = time.time()
            threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()

    def stop(self, timeout=600):
        with self.lock:
            proc = self.proc
            if proc is None or proc.poll() is not None:
                return
            log("stopping bitcoind")
            proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def restart(self):
        with self.lock:
            self.stop()
            self.next_start = 0
            self.start()

    def watch(self):
        """Run in a thread: restart bitcoind after it stops, with a growing pause."""
        while True:
            time.sleep(2)
            with self.lock:
                if not self.wanted:
                    continue
                if self.proc is not None and self.proc.poll() is not None:
                    code = self.proc.returncode
                    why = explain_exit(list(self.lines))
                    uptime = time.time() - (self.started_at or time.time())
                    self.restarts = 0 if uptime > 600 else self.restarts + 1
                    pause = min(300, 10 * (2 ** min(self.restarts, 5)))
                    self.last_exit = {"code": code, "at": time.time(), "why": why, "pause": pause}
                    log("bitcoind stopped (exit %s): %s; next try in %d s" % (code, why or "no reason given", pause))
                    self.proc = None
                    self.next_start = time.time() + pause
                if self.proc is None and time.time() >= self.next_start:
                    try:
                        self.start()
                    except OSError as err:
                        log("cannot start bitcoind:", err)
                        self.next_start = time.time() + 60


def explain_exit(lines):
    text = "\n".join(lines[-40:])
    if re.search(r"Disk space is too low|not enough disk space|No space left on device", text, re.I):
        return "disk"
    if re.search(r"Corrupted block database|Error opening block database|Fatal LevelDB error|reindex", text, re.I):
        return "database"
    if re.search(r"Cannot obtain a lock on data directory", text, re.I):
        return "lock"
    if re.search(r"Error: .*(?:config|option|parameter)", text, re.I):
        return "config"
    return None


# ---------------------------------------------------------------- app

class App:
    def __init__(self, config):
        self.config = config
        for d in (BTC_DIR, STATE_DIR):
            os.makedirs(d, exist_ok=True)
        self.state = avadoweb.read_json(STATE_FILE, {}) or {}
        self.logins = load_logins()
        self.node = Bitcoind(config)
        self.node.prepare = self.write_conf
        self.rpc = BitcoinRpc("127.0.0.1", config.rpc_port, "avado", self.logins["avado"]["password"], timeout=8)
        self.rate = avadoweb.Rate(window=3600)
        self.cache = {"at": 0, "model": None}
        self.cache_lock = threading.Lock()
        self.zero_peers_since = None
        self.decide()

    # -- mode
    def decide(self):
        free, _total = avadoweb.disk_free(DATA_DIR)
        mode, reason = decide_mode(self.config, self.state, free)
        if needs_fresh_download(self.state.get("mode"), mode):
            # MODE=full was set in the AVADO Admin on a space-saving node.
            log("MODE=full on a pruned node: deleting the pruned blocks to download the full history")
            self.delete_chain()
        if self.state.get("mode") != mode or "decidedAt" not in self.state:
            self.state.update({"mode": mode, "reason": reason, "decidedAt": int(time.time()),
                               "freeAtDecision": free})
            avadoweb.write_json(STATE_FILE, self.state)
            log("node mode: %s (%s; %s free)" % (mode, reason, avadoweb.human_bytes(free)))
        self.mode = mode

    def write_conf(self):
        avadoweb.write_text(CONF_FILE, render_conf(self.config, self.mode, self.logins), mode=0o600)

    # -- internal route for dependants
    def login_route(self, app):
        def handler(ip):
            allowed = set()
            for host in APPS[app]["hosts"] + self.config.extra_hosts.get(app, []):
                allowed |= avadoweb.resolve_ips(host)
            if ip not in allowed:
                log("refused the %s login to %s" % (app, ip))
                return 403, {"error": "This login is only for the %s app" % app}
            entry = self.logins[app]
            return 200, {"user": entry["user"], "password": entry["password"], "port": self.config.rpc_port,
                         "zmqHashBlock": "tcp://DAppNodePackage-bitcoincore.avado.dnp.dappnode.eth:28332"}
        return handler

    # -- actions
    def action_use_pruned(self, _body):
        if self.mode == "pruned":
            return {"message": "Your node already uses the space-saving mode."}
        if self.config.mode == "full":
            raise UserError("The setting MODE=full in the AVADO Admin keeps the full node. Change it there first.")
        self.state.update({"mode": "pruned", "reason": "owner", "decidedAt": int(time.time())})
        avadoweb.write_json(STATE_FILE, self.state)
        self.mode = "pruned"
        threading.Thread(target=self.node.restart, daemon=True).start()
        return {"message": "Switching to the space-saving mode. Your node restarts and frees the space in a few minutes."}

    def action_use_full(self, _body):
        if self.mode == "full":
            return {"message": "Your node already keeps the full history."}
        if self.config.mode == "pruned":
            raise UserError("The setting MODE=pruned in the AVADO Admin keeps the space-saving mode. Change it there first.")
        free, _ = avadoweb.disk_free(DATA_DIR)
        if free is None or free < FULL_HISTORY_GB * GB:
            raise UserError("Your AVADO has %s free. The full history needs about %d GB, so it does not fit."
                            % (avadoweb.human_bytes(free), FULL_HISTORY_GB))

        def switch():
            with self.node.lock:
                self.node.wanted = False
                self.node.stop()
                # A pruned node has thrown old blocks away: start the download again.
                self.delete_chain()
                self.state.update({"mode": "full", "reason": "owner", "decidedAt": int(time.time())})
                avadoweb.write_json(STATE_FILE, self.state)
                self.mode = "full"
                self.rate = avadoweb.Rate(window=3600)
                self.node.wanted = True
                self.node.next_start = 0
                self.node.start()

        threading.Thread(target=switch, daemon=True).start()
        return {"message": "Switching to the full node. It now downloads the whole Bitcoin history again."}

    def delete_chain(self):
        """Deletes the blocks, chain state and indexes (not bitcoin.conf or the logins)."""
        for sub in ("blocks", "chainstate", "indexes"):
            shutil.rmtree(os.path.join(self.chain_dir(), sub), ignore_errors=True)

    def chain_dir(self):
        return BTC_DIR if self.config.chain == "main" else os.path.join(BTC_DIR, self.config.chain)

    # -- status
    def status(self):
        with self.cache_lock:
            if self.cache["model"] is not None and time.time() - self.cache["at"] < 4:
                return self.cache["model"]
        model = self.build_model(self.gather())
        with self.cache_lock:
            self.cache = {"at": time.time(), "model": model}
        return model

    def gather(self):
        info = {"running": self.node.running(), "lastExit": self.node.last_exit, "mode": self.mode}
        free, total = avadoweb.disk_free(DATA_DIR)
        info.update({"free": free, "total": total})
        if not info["running"]:
            return info
        try:
            info["chain"] = self.rpc.call("getblockchaininfo")
            info["net"] = self.rpc.call("getnetworkinfo")
            try:
                info["mempool"] = self.rpc.call("getmempoolinfo")
            except RpcError:
                pass
        except RpcError as err:
            if err.code == RPC_IN_WARMUP:
                info["warmup"] = str(err)
            else:
                info["rpcError"] = str(err)
        return info

    def build_model(self, info):
        return build_model(info, self.config, self.state, self.rate, self, installed_apps())

    def run(self):
        signal.signal(signal.SIGTERM, self.shutdown)
        signal.signal(signal.SIGINT, self.shutdown)
        routes = {"/internal/rpc-login/%s" % app: self.login_route(app) for app in APPS if app != "avado"}
        web = avadoweb.WebApp(PUBLIC_DIR, self.status,
                              actions={"use-pruned": self.action_use_pruned, "use-full": self.action_use_full},
                              internal=routes, version=VERSION, title="Bitcoin Node")
        self.server = web.serve(port=env_int("PORT", 80))
        self.node.start()
        threading.Thread(target=self.node.watch, daemon=True).start()
        while True:
            time.sleep(3600)

    def shutdown(self, *_args):
        log("shutting down")
        self.node.wanted = False
        self.node.stop(timeout=600)
        sys.exit(0)


def installed_apps():
    """Which Bitcoin apps are installed next to this node (Docker DNS knows their containers)."""
    return {app: bool(avadoweb.resolve_ips(spec["hosts"][0])) for app, spec in APPS.items() if spec["hosts"]}


# ---------------------------------------------------------------- page model

def fmt_int(n):
    return "{:,}".format(int(n))


def build_model(info, config, state, rate, app=None, installed=None):
    installed = installed or {}
    mode = info.get("mode")
    free = info.get("free")
    chain = info.get("chain") or {}
    net = info.get("net") or {}
    pruned_text = "space-saving mode: it keeps only the newest %d GB of blocks" % config.prune_gb
    model = {"title": "Bitcoin Node", "facts": [], "sections": [], "refresh": 10}
    network_note = "" if config.chain == "main" else " (test network: %s)" % config.chain

    synced = False
    if not info.get("running"):
        last = info.get("lastExit") or {}
        why = last.get("why")
        wait = max(0, int(last.get("at", 0) + last.get("pause", 0) - time.time()))
        model["state"] = "error"
        if why == "disk":
            model["headline"] = "Your Bitcoin node stopped: the disk is full"
            model["text"] = ["There is not enough free space left on your AVADO (%s free). The node tries again by itself "
                             "every few minutes." % avadoweb.human_bytes(free),
                             "Make room by removing apps you no longer use, or switch the node to the space-saving mode below."]
        elif why == "database":
            model["headline"] = "Your Bitcoin node stopped: its block data is damaged"
            model["text"] = ["This can happen after a power cut. The node tries again by itself. If this message stays, "
                             "contact AVADO support: the node may need to rebuild its data."]
        else:
            model["headline"] = "Your Bitcoin node is starting again"
            model["text"] = ["It stopped unexpectedly and starts again in %s." % avadoweb.human_duration(wait)
                             if last else "It is starting."]
    elif info.get("warmup"):
        model["state"] = "working"
        model["headline"] = "Your Bitcoin node is starting"
        model["text"] = ["It is %s. This can take a few minutes, longer after a power cut." % warmup_text(info["warmup"])]
    elif info.get("rpcError") or not chain:
        model["state"] = "working"
        model["headline"] = "Your Bitcoin node is starting"
        model["text"] = ["The page is waiting for the node to answer."]
    else:
        blocks, headers = chain.get("blocks", 0), chain.get("headers", 0)
        progress = float(chain.get("verificationprogress") or 0)
        ibd = bool(chain.get("initialblockdownload")) or progress < 0.9999
        rate.add(progress)
        if ibd:
            model["state"] = "working"
            model["headline"] = "Your Bitcoin node is downloading the Bitcoin history%s" % network_note
            left = rate.seconds_left()
            if blocks == 0:
                # First the node fetches the list of all blocks (headers), then the blocks themselves.
                model["progress"] = {
                    "percent": 0,
                    "left": "Getting the list of all blocks first (%s so far)" % fmt_int(headers),
                    "right": "Then the blocks themselves",
                }
            else:
                model["progress"] = {
                    "percent": round(progress * 100, 2),
                    "left": "%.1f%% done, block %s of %s" % (progress * 100, fmt_int(blocks),
                                                             fmt_int(max(headers, blocks))),
                    "right": ("%s left" % avadoweb.human_duration(left)) if left else "Working out how long this takes",
                }
            text = ["Your node downloads every block since 2009 and checks each one itself. On an AVADO this takes "
                    "about 1 to 3 days. You can close this page: the download goes on."]
            if mode == "pruned":
                text.append("It is in %s. It still downloads the whole history once (about 770 GB of internet "
                            "traffic) to check it." % pruned_text)
            model["text"] = text
        else:
            synced = True
            model["state"] = "ok"
            model["headline"] = "Your Bitcoin node is up to date%s" % network_note
            model["text"] = ["It has checked every block itself and follows the network as new blocks arrive "
                             "(about every 10 minutes). Latest block: %s." % fmt_int(blocks)]
        peers = net.get("connections")
        if peers == 0:
            if app is not None:
                app.zero_peers_since = app.zero_peers_since or time.time()
                if time.time() - app.zero_peers_since > 300:
                    model["state"] = "warn"
                    model["text"].append("Your node cannot reach other Bitcoin nodes at the moment. Check that the "
                                         "AVADO is connected to the internet.")
        elif app is not None:
            app.zero_peers_since = None

    if free is not None and free < 20 * GB and info.get("running"):
        model["state"] = "warn" if model.get("state") != "error" else "error"
        model.setdefault("text", []).append("Your AVADO has only %s of free space left. When the disk is full the node "
                                            "stops." % avadoweb.human_bytes(free))

    # facts
    facts = model["facts"]
    facts.append({"k": "Mode", "v": "Full node" if mode == "full" else "Space-saving (pruned)"})
    if chain:
        facts.append({"k": "Blocks checked", "v": fmt_int(chain.get("blocks", 0))})
        facts.append({"k": "Disk used by the node", "v": avadoweb.human_bytes(chain.get("size_on_disk"))})
    if net:
        peers = net.get("connections") or 0
        facts.append({"k": "Connected to", "v": "%d Bitcoin node%s" % (peers, "" if peers == 1 else "s"),
                      "tone": "warn" if net.get("connections") == 0 else None})
    facts.append({"k": "Free on your AVADO", "v": avadoweb.human_bytes(free),
                  "tone": "warn" if free is not None and free < 50 * GB else None})

    model["sections"].append(connect_section(mode, synced, state, config, installed, free))
    model["sections"].append(explorer_section(installed))
    model["sections"].append(settings_section(mode, state, config, info, free))
    return model


def warmup_text(message):
    m = (message or "").strip().rstrip(".").rstrip("…")
    lower = m.lower()
    if "block index" in lower:
        return "loading its list of blocks"
    if "verifying" in lower:
        return "checking its newest blocks"
    if "rewinding" in lower or "replaying" in lower:
        return "catching up on blocks it had not saved before it stopped"
    if "pruning" in lower:
        return "freeing space"
    return "getting ready"


def connect_section(mode, synced, state, config, installed, free):
    s = {"id": "connect", "title": "Connect your hardware wallet to your own node"}
    if mode == "pruned":
        s["tone"] = "info"
        at = state.get("freeAtDecision")
        s["text"] = [
            "Wallets such as Sparrow and Electrum connect to your node through the Electrum Server app. The Electrum Server "
            "needs the full Bitcoin history, so it cannot run while your node is in space-saving mode.",
        ]
        if state.get("reason") == "auto" and at is not None:
            s["text"].append("Your AVADO had %s free when the node was set up. The full node and the Electrum Server "
                             "together need at least %s free." % (avadoweb.human_bytes(at),
                                                                   avadoweb.human_bytes(config.full_min_free_gb * GB)))
        if free is not None and free >= config.full_min_free_gb * GB and config.mode != "pruned":
            s["text"].append("Your AVADO now has %s free, which is enough for the full node." % avadoweb.human_bytes(free))
            s["buttons"] = [{"action": "use-full", "label": "Switch to the full node", "style": "primary",
                             "confirm": "Your node will download the whole Bitcoin history again (about 770 GB). "
                                        "This takes 1 to 3 days. Continue?"}]
        return s
    if installed.get("fulcrum"):
        s["text"] = ["Your Electrum Server is installed. Its page shows the exact address to type into Sparrow or Electrum."]
        s["links"] = [{"label": "Open the Electrum Server page", "href": "http://fulcrum.my.ava.do", "style": "primary"}]
    else:
        s["text"] = ["Wallets such as Sparrow and Electrum connect to your node through the Electrum Server app. Then "
                     "your wallet asks your own node about your coins instead of a stranger's server."]
        s["steps"] = ["Open the AVADO store (the DApp Store in the Admin).",
                      "Install \"Electrum Server\" from the Bitcoin category.",
                      "Open its page: it shows the address to type into your wallet."]
        s["links"] = [{"label": "Open the AVADO store", "href": "http://my.ava.do/#/installer", "style": "primary"}]
        if not synced:
            s["after"] = "You can install it now. It starts working when your node has finished downloading."
    return s


def explorer_section(installed):
    s = {"id": "explorer", "title": "See blocks, fees and your own transactions"}
    if installed.get("mempool"):
        s["text"] = ["The Bitcoin Explorer on your AVADO looks things up on your own node, so no website learns "
                     "which addresses you look at."]
        s["links"] = [{"label": "Open the Bitcoin Explorer", "href": "http://mempool.my.ava.do/avado/", "style": None}]
    else:
        s["text"] = ["Install the Bitcoin Explorer (mempool) from the AVADO store to see the current fees, new blocks "
                     "and your own transactions, looked up on your own node instead of a website."]
    return s


def settings_section(mode, state, config, info, free):
    chain = info.get("chain") or {}
    net = info.get("net") or {}
    s = {"id": "settings", "title": "Disk space"}
    if mode == "full":
        s["text"] = ["Your node keeps the full Bitcoin history and an index of all transactions (about 850 GB, "
                     "growing about 70 GB a year). This lets the Electrum Server run on it."]
        if config.mode != "full":
            s["buttons"] = [{"action": "use-pruned", "label": "Switch to space-saving mode", "style": None,
                             "confirm": "Your node will delete old blocks and keep only the newest %d GB. The Electrum "
                                        "Server stops working. Going back later means downloading the whole history "
                                        "again (1 to 3 days). Continue?" % config.prune_gb}]
    else:
        s["text"] = ["Your node is in space-saving mode: it keeps only the newest %d GB of blocks (about %d GB on disk "
                     "in total). It still checks every block and transaction itself." % (config.prune_gb,
                                                                                          config.prune_gb + 12)]
    adv = [
        {"k": "Network", "v": {"main": "Bitcoin (mainnet)"}.get(config.chain, config.chain)},
        {"k": "Bitcoin Core version", "v": (net.get("subversion") or "").strip("/").replace(":", " ") or "Unknown"},
        {"k": "Mode chosen", "v": {"auto": "Automatically, from the free disk space", "owner": "By you, on this page",
                                    "setting": "By the MODE setting in the AVADO Admin"}.get(state.get("reason"), "Automatically")},
        {"k": "Free when set up", "v": avadoweb.human_bytes(state.get("freeAtDecision"))},
        {"k": "Port for other Bitcoin nodes", "v": str(config.p2p_port)},
    ]
    if chain:
        adv.append({"k": "Headers known", "v": fmt_int(chain.get("headers", 0))})
        adv.append({"k": "Kept from block", "v": fmt_int(chain.get("pruneheight", 0)) if chain.get("pruned") else "0 (all)"})
    if net:
        adv.append({"k": "Connections in / out", "v": "%s / %s" % (net.get("connections_in", "?"), net.get("connections_out", "?"))})
    mempool = info.get("mempool") or {}
    if mempool:
        adv.append({"k": "Transactions waiting", "v": fmt_int(mempool.get("size", 0))})
    s["advanced"] = adv
    return s


if __name__ == "__main__":
    App(Config()).run()
