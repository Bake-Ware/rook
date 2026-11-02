"""Castling hub watchdog: tell the owner on Telegram when the 0.5 hub is down.

The 0.4 hub's watchdog duty, moved to castling (castling-switchover-decisions,
Bake: "Uptime, uptime is the focus"). Stdlib only, one file, run once a
minute by a systemd timer (castling-watchdog.service/.timer beside this file),
in two places so a dead hub, tunnel or machine still pages:

- **on the hub's machine**: ``ROOK_WATCHDOG_URL=http://127.0.0.1:8750`` and
  ``ROOK_WATCHDOG_SERVICES=rook05-hub,telesthete-hub,cloudflared``: the units
  run, ``/health`` answers, memory isn't nearly gone;
- **on another machine**: ``ROOK_WATCHDOG_URL=https://castling.bakeforge.com``
  (rook.bakeforge.com after the DNS switch), no services: the public URL answers.

It talks straight to Telegram (``ROOK_WATCHDOG_TELEGRAM_TOKEN`` / ``_CHAT``),
not through the hub, because it reports the hub being down. A failed check
must fail twice in a row before it pages (a deploy restart doesn't); each
condition pages once, again every ``ROOK_WATCHDOG_REPEAT_MIN`` (60) minutes
while it lasts, and a "recovered" follows. Settings come from the
environment (an EnvironmentFile, mode 600), never arguments; state is a JSON
file. The same env names as 0.4's watchdog, so its env file carries over.

    python3 watchdog.py --state /var/lib/castling-watchdog/state.json [--where hub] [--test-alert]
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

UA = "castling-watchdog/1"          # Cloudflare refuses urllib's own User-Agent


def env(name, default=""):
    return os.environ.get(name, default)


def health(url, timeout=15):
    """None when ``<url>/health`` says ok, else what went wrong."""
    req = urllib.request.Request(url.rstrip("/") + "/health", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode(errors="replace") or "{}")
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001 - any failure is a failed check
        return f"{type(e).__name__}: {str(e)[:120]}"
    return None if body.get("ok") else f"/health said {str(body)[:120]}"


def check(state):
    """Conditions now, as {key: message}."""
    found = {}
    for svc in [s.strip() for s in env("ROOK_WATCHDOG_SERVICES").split(",") if s.strip()]:
        r = subprocess.run(["systemctl", "is-active", svc], capture_output=True, text=True, timeout=20)
        if r.stdout.strip() != "active":
            found[f"svc:{svc}"] = f"{svc} is {r.stdout.strip() or 'unknown'}"
    url = env("ROOK_WATCHDOG_URL")
    if not url:
        return {"config": "ROOK_WATCHDOG_URL is not set (the hub's address)"}
    fail = health(url)
    state["strikes"] = state.get("strikes", 0) + 1 if fail else 0
    if fail and state["strikes"] >= 2:
        found["health"] = f"castling hub not answering at {url} ({state['strikes']} checks in a row): {fail}"
    if env("ROOK_WATCHDOG_SERVICES"):                 # on the hub's machine: its memory too
        try:
            with open("/proc/meminfo") as f:
                avail = {line.split(":")[0]: int(line.split()[1]) for line in f}["MemAvailable"] // 1024
            if avail < int(env("ROOK_WATCHDOG_MIN_MEM_MB", "80")):
                found["memory"] = f"hub memory low: {avail} MB available"
        except (OSError, KeyError, ValueError):
            pass
    return found


def telegram(text):
    tok, chat = env("ROOK_WATCHDOG_TELEGRAM_TOKEN"), env("ROOK_WATCHDOG_TELEGRAM_CHAT")
    if not tok or not chat:
        print("ALERT (telegram not configured):", text, file=sys.stderr)
        return False
    req = urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage", method="POST",
                                 data=json.dumps({"chat_id": chat, "text": text,
                                                  "disable_web_page_preview": True}).encode(),
                                 headers={"User-Agent": UA, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        print(f"telegram send failed: HTTP {e.code}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001 - never print the URL: it holds the token
        print(f"telegram send failed: {type(e).__name__}", file=sys.stderr)
    return False


def notify(state, found, where, send=None):
    """Page new conditions, remind about lasting ones, announce recoveries.
    A condition counts as paged only once a send went through."""
    send = send or telegram
    now, active = time.time(), state.setdefault("active", {})
    repeat = float(env("ROOK_WATCHDOG_REPEAT_MIN", "60")) * 60
    for key, msg in found.items():
        last = active.get(key)
        if last is None:
            if send(f"⚠️ Castling [{where}]: {msg}"):
                active[key] = {"since": now, "sent": now}
        elif now - last["sent"] >= repeat:
            mins = int((now - last["since"]) // 60)
            if send(f"⚠️ Castling [{where}]: still failing after {mins} min: {msg}"):
                last["sent"] = now
    for key in [k for k in active if k not in found]:
        if send(f"✅ Castling [{where}] recovered: {key}"):
            del active[key]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Castling hub watchdog (run once a minute).")
    ap.add_argument("--state", default="/var/lib/castling-watchdog/state.json")
    ap.add_argument("--where", default=env("ROOK_WATCHDOG_WHERE") or os.uname().nodename)
    ap.add_argument("--test-alert", action="store_true", help="send one test message and exit")
    a = ap.parse_args(argv)
    if a.test_alert:
        return 0 if telegram(f"🔔 Castling watchdog [{a.where}] test message: alerts reach you.") else 1
    try:
        with open(a.state) as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    try:
        found = check(state)
    except Exception as e:  # noqa: BLE001 - a crashed check is itself worth a page
        found = {"watchdog": f"watchdog check crashed: {type(e).__name__}: {e}"}
    notify(state, found, a.where)
    state["at"] = time.time()
    tmp = a.state + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, a.state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
