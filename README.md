# AVADO Bitcoin Node

An AVADO package that runs [Bitcoin Core](https://bitcoincore.org) (the
official release, not a fork) with a plain-language page at
`http://bitcoincore.my.ava.do`.

It is the base of the AVADO Bitcoin bundle:

| Package | Repository | Needs |
|---|---|---|
| Bitcoin Node (`bitcoincore.avado.dnp.dappnode.eth`) | this one | nothing |
| Electrum Server (`fulcrum.avado.dnp.dappnode.eth`) | AVADO-DNP-Fulcrum | Bitcoin Node, full mode |
| Bitcoin Explorer (`mempool.avado.dnp.dappnode.eth`) | AVADO-DNP-Mempool | Bitcoin Node, full mode |
| Lightning (`lightning.avado.dnp.dappnode.eth`) | AVADO-DNP-Lightning | Bitcoin Node, full mode, ZMQ |

## What it does

- **Full or space-saving, chosen from the free disk.** On the first start
  (`MODE=auto`) the node keeps the full history and a transaction index when
  the box has at least `FULL_NODE_MIN_FREE_GB` (1500 GB) free, which is what the
  Electrum Server and the explorer need. With less it starts pruned (`PRUNE_GB`, 10 GB of
  blocks). The choice is remembered; the page has a button to switch either
  way (switching to full downloads the history again and is only offered when
  it fits). `MODE=full` or `MODE=pruned` in the Admin overrides both.
- **One RPC login per app, made on the box.** On the first start the app makes
  a random password for itself, the Electrum Server and the explorer, stored
  only in this app's volume. `bitcoin.conf` holds salted hashes (`rpcauth`),
  never the passwords, and each login may call only the RPC methods its app
  needs (`rpcwhitelist`; nothing that stops the node, changes peers or
  touches a wallet). The node has no wallet (`disablewallet=1`).
- **Logins reach only their app.** The Electrum Server and the explorer fetch
  their login from `GET /internal/rpc-login/<app>` on this container. It
  answers only when the request comes from that app's own container (the
  caller's address is checked against Docker's DNS for the app's container
  name); any other caller gets 403. The page never shows a login.
- **The page** keeps the main view short (about 120 words, checked by a unit
  test): the status, the download progress and the one action that matters
  now ("Connect your hardware wallet" through the Electrum Server, or "Switch
  to the full node" on a space-saving node, whose card is `#connect`). The
  wallet steps, the explorer, disk space with "Switch to space-saving mode",
  and the numbers are behind one "Details". `/api/*` answers only requests addressed to the box (a
  `*.my.ava.do` name or an IP), buttons need the `X-Avado: 1` header, and the
  Content-Security-Policy allows no inline or foreign scripts.
- **Keeps bitcoind running** and says in plain words why it stopped (disk
  full, damaged database). Logs go to the container log only.

Ports: 8333 is published so other Bitcoin nodes can connect. RPC (8332) and
ZMQ stay inside the AVADO network: new blocks (`zmqpubhashblock`,
`zmqpubrawblock`) on 28332 and new transactions (`zmqpubrawtx`) on 28333, two
different addresses as LND requires. Lightning gets its own login like the
other apps; it may also call `getzmqnotifications` to find these addresses.

Settings (Admin, package page): `MODE` (auto/full/pruned), `PRUNE_GB`,
`FULL_NODE_MIN_FREE_GB`, `DBCACHE_MB`, `EXTRA_OPTS` (extra bitcoind options for
support; options that change the data folder, the config file or the logins
are ignored).

## Image

`build/Dockerfile` downloads the official `bitcoin-<version>-x86_64-linux-gnu.tar.gz`
from bitcoincore.org and checks its SHA-256 against the release's
`SHA256SUMS`, whose signature was verified with the builder keys in
[bitcoin-core/guix.sigs](https://github.com/bitcoin-core/guix.sigs). The page
and supervisor are Python standard library only (`build/app`); their unit
tests run during the image build. Everything runs as an unprivileged user; a
file capability lets the page open ports 80 and 443.

**HTTPS.** The page also answers on https://bitcoincore.my.ava.do (port 443) with the box's
`*.my.ava.do` certificate, the one the Admin and the other AVADO apps with https use (the
box's DAPPMANAGER serves it at http://dappmanager.my.ava.do; iso.ava.do is the second source).
Chrome tries https first ("Always use secure connections"), so without it Chrome showed "This
site doesn't support a secure connection". The certificate is used only when it is for
`*.my.ava.do`, valid for at least another hour and matches its key; until then the page stays
http only and it is checked every 5 minutes, after that daily. http works as before, also in
the Admin's Setup tab. `build/app/avadotls.py` (Python standard library only; the same file in every AVADO package with `avadoweb.py`).

## Licence and reuse

GPL-3.0 (see LICENSE). Bitcoin Core is MIT-licensed.

Prior art studied, no code copied: the older AVADO-DNP-Bitcoin package and
[dappnode/DAppNodePackage-bitcoin](https://github.com/dappnode/DAppNodePackage-bitcoin)
(GPL-3.0, Copyright DAppNode Association and contributors): the AVADO network
range for `rpcallowip`, the ZMQ ports and a wallet-less node follow their
setup. The disk-aware mode, the per-app logins and the page are new.

## Development

```
cd build/app && python3 -m unittest discover -s tests
docker buildx build --platform linux/amd64 --load -t bitcoincore-test -f build/Dockerfile .
docker run --rm -e NETWORK=regtest -e MODE=full -p 8080:80 bitcoincore-test
```

Release: a PR runs the AVADO build (GitHub Actions); merging to `main` (merge
commit, not squash) releases to the staging store. Release this package
before the Electrum Server and the explorer: the store build fails when a
dependency is not published.
