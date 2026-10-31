"""llm-openai: POST {url}/chat/completions, GET {url}/models (stdlib HTTP;
sync, so the kit runs it in a thread)."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from rook.kit._types import parse_duration
from rook.kit.ctx import KitError

STOP = {"stop": "end", "length": "length", "content_filter": "refusal"}


def complete(ctx, messages, model=None, max_tokens=None, temperature=None, stop=None):
    body = {"messages": messages}
    model = system_model(ctx.settings, model)
    if model:
        body["model"] = model
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    if temperature is not None:
        body["temperature"] = temperature
    if stop:
        body["stop"] = stop
    res = _http(ctx, "POST", "/chat/completions", body)
    try:
        choice = res["choices"][0]
        text = choice["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise KitError("the endpoint answered without a choice") from None
    usage = res.get("usage") or {}
    out = {"text": text, "model": str(res.get("model") or model or ""),
           "stop_reason": STOP.get(choice.get("finish_reason"), choice.get("finish_reason") or "end")}
    counts = {k: usage[u] for k, u in (("input", "prompt_tokens"), ("output", "completion_tokens"))
              if isinstance(usage.get(u), int)}
    return {**out, **({"usage": counts} if counts else {})}


def models(ctx):
    res = _http(ctx, "GET", "/models")
    ids = sorted(str(m.get("id")) for m in res.get("data") or [] if isinstance(m, dict) and m.get("id"))
    return {"models": ids, "systems": systems(ctx.settings)}


def systems(settings) -> dict:
    two = settings["model_two"] or ""
    one = settings["model_one"] or two
    return {"one": one, "two": two, "three": settings["model_three"] or one}


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
    url = str(ctx.settings["url"] or "").rstrip("/")
    if not url:
        raise KitError("llm-openai.url is not set")
    req = urllib.request.Request(url + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json", "Accept": "application/json"})
    if ctx.settings["key"]:
        req.add_header("Authorization", f"Bearer {ctx.settings['key']}")
    try:
        opener = urllib.request.build_opener(_NoRedirect)
        with opener.open(req, timeout=parse_duration(ctx.settings["timeout"])) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise KitError(f"{path}: HTTP {e.code}: {_error(e)}", retry=e.code == 429 or e.code >= 500) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise KitError(f"{path}: {getattr(e, 'reason', None) or type(e).__name__}", retry=True) from None
    except ValueError:
        raise KitError(f"{path}: the answer is not JSON") from None


def _error(e: urllib.error.HTTPError) -> str:
    try:
        err = json.loads(e.read() or b"{}").get("error")
    except ValueError:
        return e.reason or ""
    return str(err.get("message") if isinstance(err, dict) else err or e.reason)[:300]
