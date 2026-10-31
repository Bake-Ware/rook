"""llm-anthropic: POST {url}/v1/messages, GET {url}/v1/models (stdlib HTTP;
sync, so the kit runs it in a thread). API version 2026-10-21, which refuses
temperature, so it is never sent."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from rook.kit._types import parse_duration
from rook.kit.ctx import KitError

VERSION = "2026-10-21"
STOP = {"end_turn": "end", "max_tokens": "length", "stop_sequence": "stop"}


def complete(ctx, messages, model=None, max_tokens=None, temperature=None, stop=None):
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    turns = [{"role": m["role"], "content": m["content"]} for m in messages if m["role"] != "system"]
    if not turns:
        raise KitError("no user message to answer")
    body = {"model": system_model(ctx.settings, model), "max_tokens": max_tokens or ctx.settings["max_tokens"],
            "messages": turns}
    if system:
        body["system"] = system
    if stop:
        body["stop_sequences"] = stop
    res = _http(ctx, "POST", "/v1/messages", body)
    text = "".join(b.get("text", "") for b in res.get("content") or [] if b.get("type") == "text")
    usage = res.get("usage") or {}
    counts = {k: usage[u] for k, u in (("input", "input_tokens"), ("output", "output_tokens"))
              if isinstance(usage.get(u), int)}
    reason = res.get("stop_reason")
    return {"text": text, "model": str(res.get("model") or body["model"]),
            "stop_reason": STOP.get(reason, reason or "end"), **({"usage": counts} if counts else {})}


def models(ctx):
    res = _http(ctx, "GET", "/v1/models?limit=100")
    ids = [str(m["id"]) for m in res.get("data") or [] if isinstance(m, dict) and m.get("id")]
    return {"models": ids, "systems": systems(ctx.settings)}


def systems(settings) -> dict:
    return {s: settings[f"model_{s}"] for s in ("one", "two", "three")}


def system_model(settings, model: str | None) -> str:
    """A model id as given, or System One/Two/Three's (blank: Two)."""
    if not model or model in ("one", "two", "three"):
        return systems(settings)[model or "two"]
    return model


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib re-sends every header on a redirect, to any host: the key would
    go along. These APIs don't redirect; a 3xx comes back as an error."""

    def redirect_request(self, *args, **kwargs):
        return None


def _http(ctx, method: str, path: str, body: dict | None = None) -> dict:
    if not ctx.settings["key"]:
        raise KitError("llm-anthropic.key is not set (the vault name of an API key)")
    req = urllib.request.Request(str(ctx.settings["url"]).rstrip("/") + path, method=method,
                                 data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json", "anthropic-version": VERSION,
                                          "x-api-key": ctx.settings["key"]})
    try:
        opener = urllib.request.build_opener(_NoRedirect)
        with opener.open(req, timeout=parse_duration(ctx.settings["timeout"])) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        # 429 rate limit, 5xx and 529 overloaded: worth trying again later.
        raise KitError(f"Claude API: HTTP {e.code}: {_error(e)}", retry=e.code == 429 or e.code >= 500) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise KitError(f"Claude API: {getattr(e, 'reason', None) or type(e).__name__}", retry=True) from None
    except ValueError:
        raise KitError("Claude API: the answer is not JSON") from None


def _error(e: urllib.error.HTTPError) -> str:
    try:
        err = json.loads(e.read() or b"{}").get("error") or {}
    except ValueError:
        return e.reason or ""
    return str(err.get("message") if isinstance(err, dict) else err)[:300]
