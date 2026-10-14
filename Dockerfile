# Rook hub image: the telesthete UDP relay and the hub (dashboard + MCP server
# in one process, rook-hub). One image, two processes; compose.yaml runs each
# as its own service. rook-dashboard / rook-mcp are still installed (compat).
# Published multi-arch (amd64, arm64) as ghcr.io/bake-ware/rook by
# .github/workflows/release.yml.
#
#   docker build -t rook-hub .                                   # the pinned prebuilt relay
#   docker build --build-arg TELESTHETE_RELAY_VERSION=v0.3.0 .   # pin the relay
#   docker build --build-arg RELAY_SOURCE=source .               # compile the relay instead

# Base images, pinned by digest (multi-arch manifest lists; refresh both the
# tag and the digest together).
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
ARG RUST_IMAGE=rust:1-slim-bookworm@sha256:2c3a22f0a5533ea2dd5a16627bc841228151faa2d4de2644ac9987e4a2f1f2fa
# Which telesthete release's prebuilt, static relay to use. Must equal RELAY in
# rook/_release.py, whose RELAY_SHA256 the download is checked against (move
# both with `python tools/release_prep.py --pin-relay vX.Y.Z`). It must speak
# the same wire as the telesthete pinned in pyproject.toml.
ARG TELESTHETE_RELAY_VERSION=v0.3.0
# "release" downloads that binary (seconds, sha256-verified); "source" compiles
# it with cargo at TELESTHETE_REV (minutes; for unreleased relay changes or an
# arch without a prebuilt).
ARG RELAY_SOURCE=release
# Source fallback only. Keep in lockstep with the telesthete pin in
# pyproject.toml (both move to the merged telesthete main commit once the
# audit/hub PR lands).
ARG TELESTHETE_REV=f3419d2292f7fe193fa3205880186823b78b16c1

# --- relay, option 1: prebuilt binary from the telesthete GitHub Release -----
# Runs on the build machine's own arch (no emulation) and fetches the binary
# for the target arch.
# hadolint ignore=DL3006
FROM --platform=$BUILDPLATFORM ${PYTHON_IMAGE} AS relay-release
ARG TARGETARCH
ARG TARGETVARIANT
ARG TELESTHETE_RELAY_VERSION
ARG TELESTHETE_RELAY_BASE_URL=https://github.com/Bake-Ware/telesthete/releases
COPY scripts/fetch-relay.py /fetch-relay.py
COPY rook/_release.py /rook_release.py
RUN python3 /fetch-relay.py --version "${TELESTHETE_RELAY_VERSION}" --pins /rook_release.py \
      --docker-arch "${TARGETARCH}${TARGETVARIANT:+/${TARGETVARIANT}}" \
      --base-url "${TELESTHETE_RELAY_BASE_URL}" --dest /out/bin

# --- relay, option 2: build telesthete-hub from source -------------------------
# hadolint ignore=DL3006
FROM ${RUST_IMAGE} AS relay-source
ARG TELESTHETE_REV
# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN cargo install --locked --root /out \
      --git https://github.com/Bake-Ware/telesthete --rev "${TELESTHETE_REV}" telesthitium

# BuildKit builds only the stage this selects.
# hadolint ignore=DL3006
FROM relay-${RELAY_SOURCE} AS relay

# --- runtime ---------------------------------------------------------------
# hadolint ignore=DL3006
FROM ${PYTHON_IMAGE}
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ROOK_DATA_DIR=/data

WORKDIR /src
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY integrations/claude-code/.claude-plugin/plugin.json integrations/claude-code/.claude-plugin/plugin.json
COPY rook ./rook
COPY skills ./skills
# Packaged as rook/_voice_engine (voice plugin; its extras are not installed here).
COPY services/voice ./services/voice
# A source checkout pins telesthete as git+https, which needs git to fetch; a
# release build (tools/release_prep.py) pins a hash-checked wheel URL instead,
# so git is installed (and removed again) only when it is needed.
# hadolint ignore=DL3008
RUN if grep -q 'telesthete @ git+' pyproject.toml; then \
      apt-get update && apt-get install -y --no-install-recommends git; \
    fi \
 && pip install . \
 && if command -v git >/dev/null; then \
      apt-get purge -y git && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*; \
    fi

COPY --from=relay /out/bin/telesthete-hub /usr/local/bin/telesthete-hub

RUN useradd --system --uid 10001 --home-dir /data rook \
 && mkdir -p /data && chown rook /data && chmod 700 /data
USER 10001
WORKDIR /data
VOLUME /data

LABEL org.opencontainers.image.source="https://github.com/Bake-Ware/rook" \
      org.opencontainers.image.description="Rook hub: band relay, dashboard and MCP server" \
      org.opencontainers.image.licenses="Apache-2.0"

# 7005 dashboard + MCP + /band, 8765 MCP + /band alone (old layout), 7474/udp relay
EXPOSE 7005 8765 7474/udp
CMD ["rook-hub"]
