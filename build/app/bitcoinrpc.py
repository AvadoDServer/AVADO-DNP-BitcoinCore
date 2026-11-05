"""Minimal Bitcoin Core JSON-RPC client (standard library only). The same file is
in AVADO-DNP-BitcoinCore, AVADO-DNP-Fulcrum and AVADO-DNP-Mempool; keep the
copies identical."""

import base64
import http.client
import json


class RpcError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


# Bitcoin Core answers -28 while it is still starting (loading the block index,
# verifying blocks, ...). The message says what it is doing.
RPC_IN_WARMUP = -28


def read_cookie(path):
    """(user, password) from Bitcoin Core's .cookie file, or None."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            user, _, password = handle.read().strip().partition(":")
        return (user, password) if password else None
    except OSError:
        return None


class BitcoinRpc:
    def __init__(self, host, port, user, password, timeout=10):
        self.host = host
        self.port = int(port)
        token = base64.b64encode(("%s:%s" % (user, password)).encode("utf-8")).decode("ascii")
        self.auth = "Basic " + token
        self.timeout = timeout

    def call(self, method, *params):
        body = json.dumps({"jsonrpc": "1.0", "id": "avado", "method": method, "params": list(params)})
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            conn.request("POST", "/", body, {"Content-Type": "application/json", "Authorization": self.auth})
            resp = conn.getresponse()
            raw = resp.read()
        except (OSError, http.client.HTTPException) as err:
            raise RpcError("Cannot reach Bitcoin Core: %s" % err) from err
        finally:
            conn.close()
        if resp.status == 401:
            raise RpcError("Bitcoin Core refused the login", code=401)
        if resp.status == 403:
            raise RpcError("Bitcoin Core does not allow %s for this app" % method, code=403)
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError as err:
            raise RpcError("Unexpected answer from Bitcoin Core (HTTP %d)" % resp.status) from err
        if data.get("error"):
            err = data["error"]
            raise RpcError(err.get("message", "error"), code=err.get("code"))
        return data.get("result")


def fetch_login(host, app, port=80, timeout=10):
    """The RPC login the AVADO Bitcoin Node made for this app: (user, password, rpc port).

    The Bitcoin Node answers only when the request comes from the container of
    that app (it checks the caller's address against Docker's DNS), so another
    app cannot read the login. Raises RpcError when the node is not reachable
    or refuses."""
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", "/internal/rpc-login/%s" % app, headers={"Host": "bitcoincore.my.ava.do"})
        resp = conn.getresponse()
        raw = resp.read()
    except (OSError, http.client.HTTPException) as err:
        raise RpcError("Cannot reach the Bitcoin Node app: %s" % err) from err
    finally:
        conn.close()
    if resp.status != 200:
        raise RpcError("The Bitcoin Node app refused to give a login (HTTP %d)" % resp.status, code=resp.status)
    try:
        data = json.loads(raw.decode("utf-8"))
        return data["user"], data["password"], int(data.get("port") or 8332)
    except (ValueError, KeyError, TypeError) as err:
        raise RpcError("Unexpected answer from the Bitcoin Node app") from err
