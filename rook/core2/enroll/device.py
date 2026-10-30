"""The device's side of enrollment (spec 12): find the hub, sign in as a
person, and write what the worker needs to start.

    python -m rook.core2.worker --config worker.yaml enroll https://rook.example     # shows a code to approve
    python -m rook.core2.worker --config worker.yaml enroll 'rook://enroll?url=…&code=…&hub=…'   # the QR's text
    python -m rook.core2.worker --config worker.yaml enroll --discover                # look on the local network

Finding the hub, in the spec's order: the QR (address, one-time code and hub
id), local network discovery (:mod:`.mdns`), a typed address. Without a
one-time code the device asks for a device code and prints the short user
code; the person approves it on their phone or the dashboard.

The bundle travels over TLS only: plain ``http://`` is refused unless the hub
is on this machine (loopback) or ``--insecure`` says so. The hub id from a
QR is pinned: a bundle from any other hub is refused.

What it writes, beside the config (paths from the config when it has them):
``anchor.json`` (the home hub's ``{hub, root, band}``, spec 9.2),
``<data>/bands.json`` (the home band's PSK and relay; the worker joins every
band listed there), ``<data>/services.json`` (service endpoints and keys, when
the bundle has any) and ``<data>/enrollment.json`` (the hub's address, the
owner). The config gets ``data``, ``anchor``, ``name`` and ``worker_id``;
everything else in it is kept. Enrolling again needs ``force``, which
re-anchors the worker and drops every other band (9.5).
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

import yaml

from .. import wire
from .hub import CODE_SECS, NAME

HTTP_SECS = 30.0


class EnrollError(Exception):
    pass


def parse_target(text: str) -> tuple[str, str | None, str | None]:
    """A typed address or a QR's text -> (url, one-time code, pinned hub id)."""
    if text.startswith("rook://enroll"):
        q = parse_qs(urlsplit(text).query)
        url = (q.get("url") or [""])[0]
        if not url:
            raise EnrollError("the QR has no hub address")
        return url.rstrip("/"), (q.get("code") or [None])[0], (q.get("hub") or [None])[0]
    return (text if "://" in text else f"https://{text}").rstrip("/"), None, None


def check_url(url: str, insecure: bool = False) -> None:
    parts = urlsplit(url)
    if parts.scheme == "https" or insecure:
        return
    if parts.scheme != "http":
        raise EnrollError(f"not a hub address: {url}")
    host = parts.hostname or ""
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise EnrollError(f"{url} is plain http: the band key would cross the network in the clear. "
                          "Use the hub's https address, or --insecure if you mean it")


def _post(url: str, path: str, body: dict) -> dict:
    req = urllib.request.Request(url + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_SECS) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return {"ok": False, **json.loads(e.read() or b"{}")}
        except ValueError:
            return {"ok": False, "error": f"HTTP {e.code}"}
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise EnrollError(f"can't reach the hub at {url}: {getattr(e, 'reason', e)}") from None


def find_hub(discover: Callable[[], list]) -> tuple[str, str]:
    """The one hub on the local network: (url, hub id)."""
    hubs = discover()
    if not hubs:
        raise EnrollError("no Rook hub answered on the local network; type its address")
    if len(hubs) > 1:
        raise EnrollError("several hubs answered; type the one you mean:\n"
                          + "\n".join(f"  {h['url']}  (hub {h['hub']})" for h in hubs))
    return hubs[0]["url"].rstrip("/"), hubs[0]["hub"]


def check_bundle(bundle: Any, pinned: str | None) -> dict:
    """The bundle's parts hang together, and it is from the pinned hub."""
    try:
        hub, band = bundle["hub"], bundle["band"]
        if wire.key_id(hub["root"]) != hub["hub_id"]:
            raise EnrollError("the bundle's hub id is not its root key's")
        from rook.core2.transport import band_of
        if band_of(band["psk"]) != band["id"]:
            raise EnrollError("the bundle's band key is for another band")
        if not (band.get("relay") or band.get("ws_url")):
            raise EnrollError("the bundle says no way to reach the band")
        if not (isinstance(bundle["worker_id"], str) and len(bundle["worker_id"]) == 32):
            raise EnrollError("the bundle has no worker id")
    except (KeyError, TypeError, ValueError, AttributeError, IndexError) as e:
        raise EnrollError(f"the hub sent a malformed bundle ({e})") from None
    if pinned and hub["hub_id"] != pinned:
        raise EnrollError(f"the bundle is from hub {hub['hub_id']}, not {pinned}: refused")
    return bundle


def sign_in(url: str, name: str, code: str | None = None, pinned: str | None = None, *,
            say: Callable[[str], None] = print, sleep: Callable[[float], None] = time.sleep,
            clock: Callable[[], float] = time.monotonic) -> dict:
    """Claim the one-time ``code``, or wait for the person to approve a
    device code; returns the checked bundle."""
    if code:
        got = _post(url, "/enroll/claim", {"code": code, "name": name})
    else:
        start = _post(url, "/enroll/device", {"name": name})
        if not start.get("ok"):
            raise EnrollError(f"the hub refused: {start.get('error')}")
        if not (isinstance(start.get("device_code"), str) and isinstance(start.get("user_code"), str)):
            raise EnrollError("the hub's answer has no device code")
        pinned = pinned or start.get("hub")
        say(f"To add {name}, approve code {start['user_code']} on hub {start.get('hub')} "
            "(the Rook app, the dashboard, or POST /enroll/approve).")
        deadline = clock() + CODE_SECS       # the hub's own expiry ends it sooner
        while True:
            got = _post(url, "/enroll/poll", {"device_code": start["device_code"]})
            if not got.get("pending"):
                break
            if clock() > deadline:
                raise EnrollError("the code expired before anyone approved it")
            interval = got.get("interval")
            sleep(min(max(interval if isinstance(interval, (int, float)) else 2, 1), 10))
    if not got.get("ok"):
        raise EnrollError(f"the hub refused: {got.get('error')}")
    return check_bundle(got.get("bundle"), pinned)


def _write(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _config(config: Path) -> dict:
    try:
        raw = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}
    if not isinstance(raw, dict):
        raise EnrollError(f"{config}: not a mapping")
    return raw


def default_name(config: Path) -> str:
    name = _config(config).get("name") or socket.gethostname().split(".")[0]
    return name if isinstance(name, str) and NAME.fullmatch(name) and name != "rook" else "worker"


def home_of(config: Path) -> str | None:
    """The hub this worker is enrolled with already (its anchor's), or None."""
    raw = _config(config)
    anchor = config.resolve().parent / (raw.get("anchor") or "anchor.json")
    if not anchor.exists():
        return None
    try:
        return str(json.loads(anchor.read_text(encoding="utf-8")).get("hub"))
    except (OSError, ValueError, AttributeError):
        return "?"


def already(hub: str | None) -> str:
    return (f"this worker's home is already hub {hub}. Enrolling again (--force) makes the new hub "
            "its home and drops every other band")


def install(config: Path, bundle: dict, url: str, force: bool = False) -> dict:
    """Write the anchor, the home band and the config; returns the paths."""
    raw = _config(config)
    base = config.resolve().parent
    data = base / (raw.get("data") or "data")
    anchor = base / (raw.get("anchor") or "anchor.json")
    memberships = base / raw["memberships"] if raw.get("memberships") else data / "memberships.json"
    if anchor.exists():
        if not force:
            raise EnrollError(already(home_of(config)))
        for old in (memberships, data / "bands.json", data / "services.json"):
            old.unlink(missing_ok=True)
        anchor.unlink()
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    band, hub = bundle["band"], bundle["hub"]
    # The worker only reads it. A service install makes it root's (spec 9.2);
    # a user install can't protect it from the worker's own account, and the
    # worker says so when it starts.
    _write(anchor, json.dumps({"hub": hub["hub_id"], "root": hub["root"], "band": band["id"]}) + "\n", 0o644)
    link = {k: band[k] for k in ("psk", "relay", "ws_url") if band.get(k)}
    _write(data / "bands.json", json.dumps({band["id"]: link}, indent=1), 0o600)
    if bundle.get("services"):
        _write(data / "services.json", json.dumps(bundle["services"], indent=1), 0o600)
    _write(data / "enrollment.json", json.dumps({"hub": hub["hub_id"], "url": hub.get("url") or url,
                                                 "owner": bundle.get("owner"), "at": int(time.time())},
                                                indent=1), 0o600)
    rel = lambda p: os.path.relpath(p, base)      # noqa: E731
    raw.update(data=rel(data), anchor=rel(anchor), name=bundle["name"], worker_id=bundle["worker_id"])
    _write(config, yaml.safe_dump(raw, sort_keys=False), 0o600)
    return {"config": str(config), "anchor": str(anchor), "data": str(data)}
