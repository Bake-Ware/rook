"""Ready-made jobs: templates an owner (or an agent for them) installs with
one call (docs/design/routines.md).

``job.read templates`` lists them with what each needs and whether one is
installed; ``job.write install`` (``id`` = the template, ``data`` =
``{config, name?, schedule?, enabled?}``) builds the job and saves it through
``create``, so it is an ordinary job: the caller owns it, it runs as its
creator (rechecked each run), it follows the default guardrails, and it shows
on the Jobs page like any other. Nothing is installed by default.

Each template is one ``cap`` step on worker ``rook`` calling a ``routine.*``
cap with the config as its args (``{{secret:name}}`` placeholders stay
placeholders in the saved job and resolve only at the call). ``vars.template``
records which template made the job.
"""
from __future__ import annotations

import copy
import re
from typing import Any

VERSION = 1

TEMPLATES: dict[str, dict] = {
    "security-scan": {
        "title": "Security scan",
        "what": ("Every 5 minutes: what is open on the band (workers, console rooms, MCP identities, "
                 "dashboard sign-ins) and what the logs say (denied calls, refused keys, new keys, "
                 "access changes), compared with the last scan. Quiet unless something changed; urgent "
                 "only for a burst of refused keys."),
        "cap": "routine.security_scan",
        "schedule": "*/5 * * * *",
        "timeout": "2m",
        "retention_days": 3,
        "needs": {},
        "options": {"urgent_refusals": "refused MCP calls within the window that make it urgent "
                                       "(default 10; 0: never urgent)",
                    "refusal_window_min": "the window, in minutes (default 10)",
                    "urgent_cooldown_min": "at most one urgent alert this often, in minutes (default 60)"},
        "example": {},
    },
    "attention-digest": {
        "title": "Attention digest",
        "what": ("Every 2 hours from 8:00 to 20:00: what came in since the last digest from your phones "
                 "(their quiet feed), chat rooms (Telegram and Discord included) and email when a "
                 "source is set up, as one quiet message. Nothing new: nothing is sent."),
        "cap": "routine.attention_digest",
        "schedule": "0 8-20/2 * * *",
        "timeout": "3m",
        "retention_days": 14,
        "needs": {},
        "options": {
            "phones": "phone workers (default: Settings > Attention phones, else every phone)",
            "chat_as": "whose unread rooms (default user:operator, the dashboard)",
            "email": ("{imap: {host, user, password: \"{{secret:name}}\", port?, folder?}} or "
                      "{cap: {worker, cap, args?}}; none: email only from mail apps' phone "
                      "notifications"),
            "max_items": "lines per section (default 8)",
        },
        "example": {"email": {"imap": {"host": "imap.example.com", "user": "me@example.com",
                                       "password": "{{secret:imap-password}}"}}},
    },
    "camera-review": {
        "title": "Camera review",
        "what": ("Every 10 minutes: a frame from each camera, a cheap change test on the hub, and a "
                 "vision model only for frames that changed. Tells you quietly about notable events "
                 "(people, vehicles, packages, doors, smoke). Ring: bring a bridge such as ring-mqtt "
                 "or Scrypted that exposes RTSP or a snapshot URL."),
        "cap": "routine.camera_review",
        "schedule": "*/10 * * * *",
        "timeout": "10m",
        "retention_days": 7,
        "needs": {
            "cameras": ("a list: {name, url} (a JPEG snapshot URL, headers?), {name, worker, rtsp} "
                        "(ffmpeg on that worker) or {name, worker, camera} (camera.snap); "
                        "credentials as {{secret:name}}"),
            "provider": "a model provider id (Intelligence sources > Model providers) with a vision model",
        },
        "options": {
            "model": "the vision model id (default: the provider's default model)",
            "threshold": "how much a frame must change, 0-1 (default 0.06)",
            "note": "what matters to you, passed to the model",
            "may_ring": "let fire, smoke, flood or a break-in ring the phone (default off: always quiet)",
        },
        "example": {"cameras": [{"name": "porch", "url": "{{secret:porch-snapshot-url}}"},
                                {"name": "garage", "worker": "nas", "rtsp": "{{secret:garage-rtsp}}"}],
                    "provider": "lab-gpu", "model": "qwen2.5-vl"},
    },
}

OWNERS_ONLY = {"read": "human:owner", "edit": "human:owner", "run": "human:owner"}
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def template(name: Any) -> dict:
    t = TEMPLATES.get(str(name or "").strip().lower())
    if t is None:
        raise KeyError(f"no template {name!r} (templates: {', '.join(TEMPLATES)})")
    return t


def describe(name: str, t: dict) -> dict:
    return {"template": name, "title": t["title"], "what": t["what"], "cap": t["cap"],
            "schedule": t["schedule"], "zero_config": not t["needs"], "needs": dict(t["needs"]),
            "options": dict(t["options"]), "example": copy.deepcopy(t["example"]),
            "version": VERSION}


def build(name: str, data: dict) -> dict:
    """The job ``install`` saves. Raises ValueError saying plainly what is missing."""
    t = template(name)
    name = str(name).strip().lower()
    config = data.get("config", {})
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise ValueError("data.config: an object with the template's settings")
    allowed = set(t["needs"]) | set(t["options"])
    unknown = sorted(set(config) - allowed)
    if unknown:
        raise ValueError(f"{name} does not take {', '.join(unknown)} (it takes: "
                         f"{', '.join(sorted(allowed)) or 'nothing'})")
    missing = [k for k in t["needs"] if config.get(k) in (None, "", [], {})]
    if missing:
        raise ValueError(f"{name} needs " + "; ".join(f"{k}: {t['needs'][k]}" for k in missing))
    job_name = str(data.get("name") or name).strip()
    if not NAME_RE.match(job_name):
        raise ValueError("data.name: 1-64 lowercase letters, digits, ., - or _")
    schedule = str(data.get("schedule") or t["schedule"]).strip()
    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("data.enabled: true or false")
    return {
        "name": job_name,
        "description": f"{t['title']} (ready-made job). {t['what']}",
        "enabled": enabled,
        "triggers": [{"kind": "cron", "expr": schedule}],
        "overlap": {"mode": "skip"},
        "missed": {"mode": "skip"},
        "retention_days": t["retention_days"],
        # Their runs hold the band's security picture and the person's messages:
        # only owners (and the operator, who always may) read or run them.
        "access": dict(OWNERS_ONLY),
        "entry": "run",
        "steps": {"run": {"kind": "cap", "worker": "rook", "cap": t["cap"], "args": copy.deepcopy(config),
                          "timeout": t["timeout"]}},
        "vars": {"template": name, "template_version": VERSION},
    }
