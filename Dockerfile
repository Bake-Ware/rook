# Rook hub image: the telesthete UDP relay, the dashboard and the MCP server.
# One image, three processes; compose.yaml runs each as its own service.

ARG TELESTHETE_REV=b6290c7afabe14c0e81d4e9adfd5bcf498a251e5

# --- relay: build telesthete-hub from source --------------------------------
FROM rust:1-slim-bookworm AS relay
ARG TELESTHETE_REV
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN cargo install --locked --root /out \
      --git https://github.com/Bake-Ware/telesthete --rev "${TELESTHETE_REV}" telesthitium

# --- runtime ---------------------------------------------------------------
FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ROOK_DATA_DIR=/data

WORKDIR /src
COPY pyproject.toml README.md ./
COPY rook ./rook
# git is only needed to fetch the telesthete dependency.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && pip install . \
 && apt-get purge -y git && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

COPY --from=relay /out/bin/telesthete-hub /usr/local/bin/telesthete-hub

RUN useradd --system --uid 10001 --home-dir /data rook \
 && mkdir -p /data && chown rook /data && chmod 700 /data
USER rook
WORKDIR /data
VOLUME /data

# 7005 dashboard, 8765 MCP + WebSocket worker bridge, 7474/udp relay
EXPOSE 7005 8765 7474/udp
CMD ["rook-dashboard"]
