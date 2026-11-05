"""One-shot WAMP v2 call to the AVADO DAPPMANAGER (standard library only).

Used only for getParams, to learn the box's address on the home network
(`internalip`), so the page can show the exact address to type into a wallet.
The router at ws://wamp.my.ava.do:8080/ws (realm dappnode_admin) accepts
anonymous sessions; the same is done by the AVADO Care package.
"""

import base64
import json
import os
import socket
import struct

HELLO, WELCOME, ABORT, GOODBYE, ERROR, CALL, RESULT = 1, 2, 3, 6, 8, 48, 50


class WampError(Exception):
    pass


def _recv_exact(sock, count):
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise WampError("connection closed")
        data += chunk
    return data


def _send_frame(sock, payload, opcode=1):
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    header = bytes([0x80 | opcode])
    length = len(data)
    if length < 126:
        header += bytes([0x80 | length])
    elif length < 65536:
        header += bytes([0x80 | 126]) + struct.pack(">H", length)
    else:
        header += bytes([0x80 | 127]) + struct.pack(">Q", length)
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    sock.sendall(header + mask + masked)


def _recv_message(sock):
    message = b""
    while True:
        b1, b2 = _recv_exact(sock, 2)
        fin, opcode = b1 & 0x80, b1 & 0x0F
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack(">H", _recv_exact(sock, 2))[0]
        elif length == 127:
            length = struct.unpack(">Q", _recv_exact(sock, 8))[0]
        if length > 4 * 1024 * 1024:
            raise WampError("message too large")
        mask = _recv_exact(sock, 4) if b2 & 0x80 else None
        data = _recv_exact(sock, length)
        if mask:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        if opcode == 8:
            raise WampError("closed by router")
        if opcode == 9:  # ping
            _send_frame(sock, data, opcode=10)
            continue
        if opcode == 10:
            continue
        message += data
        if fin:
            return json.loads(message.decode("utf-8"))


def call_dappmanager(method, host="wamp.my.ava.do", port=8080, timeout=10):
    """Calls <method>.dappmanager.dnp.dappnode.eth and returns its `result`."""
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            "GET /ws HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: wamp.2.json\r\n\r\n"
        ) % (host, port, key)
        sock.sendall(request.encode("ascii"))
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(1024)
            if not chunk:
                raise WampError("no handshake answer")
            head += chunk
            if len(head) > 16384:
                raise WampError("handshake too long")
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise WampError("handshake refused")
        _send_frame(sock, json.dumps([HELLO, "dappnode_admin", {"roles": {"caller": {}}}]))
        welcome = _recv_message(sock)
        if not welcome or welcome[0] != WELCOME:
            raise WampError("no WELCOME")
        procedure = "%s.dappmanager.dnp.dappnode.eth" % method
        _send_frame(sock, json.dumps([CALL, 1, {}, procedure, [], {"dontLogError": True}]))
        while True:
            msg = _recv_message(sock)
            if msg[0] == RESULT and msg[1] == 1:
                break
            if msg[0] == ERROR:
                raise WampError("call failed: %s" % (msg[4] if len(msg) > 4 else "error"))
        try:
            _send_frame(sock, json.dumps([GOODBYE, {}, "wamp.close.normal"]))
        except OSError:
            pass
        raw = msg[3][0] if len(msg) > 3 and msg[3] else None
        env = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(env, dict) or env.get("success") is not True:
            raise WampError("%s: not successful" % method)
        return env.get("result")
    finally:
        sock.close()


def box_lan_ip(host="wamp.my.ava.do"):
    """The box's address on the home network, or None."""
    try:
        params = call_dappmanager("getParams", host=host)
    except (OSError, ValueError, WampError, IndexError, TypeError):
        return None
    ip = (params or {}).get("internalip")
    if isinstance(ip, str) and ip.count(".") == 3 and all(p.isdigit() for p in ip.split(".")):
        return ip
    return None
