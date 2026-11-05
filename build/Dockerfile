# AVADO Bitcoin Node image: Bitcoin Core plus the AVADO status page.
#
# Build context is the repository root (docker-compose.yml: context ".",
# dockerfile "build/Dockerfile"), so the image can read the package version
# from dappnode_package.json. See /.dockerignore for what is sent.

# Base image pinned by digest (multi-arch index digest). Update tag and digest
# together: `docker buildx imagetools inspect debian:<tag>` ("Digest:" line).
ARG DEBIAN_IMAGE=debian:bookworm-20261027-slim@sha256:99c0b25c46d0214d5a2091092e7908907dcaecd6a962cde0ac700acee222b6f3

########## Bitcoin Core, verified
FROM --platform=linux/amd64 ${DEBIAN_IMAGE} AS bitcoin
# The official release from bitcoincore.org. The SHA-256 below is the line for
# this file in the release's SHA256SUMS, whose signature was checked with gpg
# against the builder keys in github.com/bitcoin-core/guix.sigs (at least 5
# good signatures; 8 for 32.1, among them fanquake, achow101 and Sjors). A
# different download fails the build. To update: change both lines together.
# The weekly upstream check (.github/workflows/upstream-check.yml) does this
# check for each new version and lists the signers in its PR.
ARG BITCOIN_VERSION=32.1
ARG BITCOIN_SHA256=61545c17c3120fb7412f896d248b11993af04ec8817aad3649c55b2afb044963
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
RUN set -eux; \
    file="bitcoin-${BITCOIN_VERSION}-x86_64-linux-gnu.tar.gz"; \
    curl -fsSL --retry 5 "https://bitcoincore.org/bin/bitcoin-core-${BITCOIN_VERSION}/${file}" -o /tmp/bitcoin.tar.gz; \
    echo "${BITCOIN_SHA256}  /tmp/bitcoin.tar.gz" | sha256sum -c -; \
    mkdir -p /opt/bitcoin; \
    tar -xzf /tmp/bitcoin.tar.gz -C /opt/bitcoin --strip-components=1; \
    /opt/bitcoin/bin/bitcoind -version | head -1 | grep -q "v${BITCOIN_VERSION}"

########## Runtime
FROM --platform=linux/amd64 ${DEBIAN_IMAGE}
# python3: the status page and supervisor (standard library only).
# libcap2-bin only to give python the right to open port 80 without root
# (file capability, like the AVADO Care package does for node), then removed.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 libcap2-bin tini \
    && setcap cap_net_bind_service=+ep "$(readlink -f /usr/bin/python3)" \
    && getcap "$(readlink -f /usr/bin/python3)" | grep -q cap_net_bind_service \
    && apt-get purge -y libcap2-bin && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY --from=bitcoin /opt/bitcoin/bin/bitcoind /opt/bitcoin/bin/bitcoin-cli /usr/local/bin/
COPY build/app /app
COPY dappnode_package.json /tmp/dappnode_package.json
RUN python3 -c 'import json,re; v=json.load(open("/tmp/dappnode_package.json"))["version"]; \
assert re.fullmatch(r"[0-9A-Za-z.+-]+", v), "bad package version"; open("/app/package-version","w").write(v+"\n")' \
    && rm /tmp/dappnode_package.json \
    && cd /app && python3 -m unittest discover -s tests -q \
    && rm -rf /app/tests /app/__pycache__ \
    && useradd --uid 1000 --create-home --shell /usr/sbin/nologin bitcoin \
    && mkdir -p /data && chown bitcoin:bitcoin /data \
    && ln -s /data/bitcoin /home/bitcoin/.bitcoin

# Not root: the page binds port 80 through the file capability above, and a new
# volume starts out owned by `bitcoin` because /data is.
USER bitcoin
ENV DATA_DIR=/data PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
VOLUME /data
EXPOSE 80 8332 8333 28332 28333
# tini reaps processes and passes the stop signal on; main.py then stops
# bitcoind cleanly.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "/app/main.py"]
