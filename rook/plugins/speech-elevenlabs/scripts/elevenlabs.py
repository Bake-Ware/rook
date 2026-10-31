"""speech-elevenlabs: GET {url}/v1/voices, POST {url}/v1/text-to-speech/{id}
(stdlib HTTP; sync, so the kit runs it in a thread). Audio comes back as raw
24 kHz PCM (output_format pcm_24000) and goes out as a WAV file, the same
format as the speech engine's."""
from __future__ import annotations

import io
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import wave

from rook.kit._types import parse_duration
from rook.kit.ctx import KitError

RATE = 24000
MAX_VOICES = 40
LIST_TTL = 600.0          # the account's voice list is asked again after this many seconds
_listed: dict = {}        # (url, key) -> (time, ids)


def tts(ctx, text, voice=None):
    ids = _voices(ctx)
    name = voice or ctx.settings["default"] or next(iter(sorted(ids)), None)
    if name not in ids:
        raise KitError(f"no ElevenLabs voice {name!r}; see speech-elevenlabs.voices")
    path = f"/v1/text-to-speech/{urllib.parse.quote(ids[name][1])}?output_format=pcm_{RATE}"
    pcm = _http(ctx, "POST", path, {"text": text, "model_id": ctx.settings["model"]}, raw=True)
    if len(pcm) < 2:
        raise KitError("ElevenLabs returned no audio", retry=True)
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm[: len(pcm) // 2 * 2])
    return ctx.file(data=out.getvalue(), name="speech.wav", mime="audio/wav")


def voices(ctx):
    ids = _voices(ctx)
    names = sorted(ids)
    default = ctx.settings["default"] if ctx.settings["default"] in ids else next(iter(names), None)
    return {"voices": names, "default": default,
            "catalog": [{"id": v, "engine": "elevenlabs", "label": f"{ids[v][0]} (ElevenLabs)"} for v in names]}


def parse_voice_map(raw: str) -> dict:
    """``'Rachel=21m00Tcm4TlvDq8ntWcQ, adam'`` -> ``{'eleven_rachel': ('Rachel', '21m...'), ...}``
    (the 0.4 voice service's names); a bare entry is a voice id named after itself."""
    out = {}
    for part in str(raw or "").split(","):
        label, sep, voice_id = part.strip().partition("=")
        label, voice_id = label.strip(), (voice_id if sep else label).strip()
        slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:40]
        if slug and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", voice_id):
            out["eleven_" + slug] = (label, voice_id)
    return out


def _voices(ctx) -> dict:
    """The configured voices, else the account's (kept LIST_TTL seconds)."""
    ids = parse_voice_map(ctx.settings["voices"])
    if ids:
        return ids
    held = _listed.get((ctx.settings["url"], ctx.settings["key"]))
    if held and time.monotonic() - held[0] < LIST_TTL:
        return held[1]
    got = _http(ctx, "GET", "/v1/voices")
    for v in (got.get("voices") or [])[:MAX_VOICES]:
        if isinstance(v, dict) and v.get("voice_id") and v.get("name"):
            ids.update(parse_voice_map(f"{v['name']}={v['voice_id']}"))
    if not ids:
        raise KitError("the ElevenLabs account has no voices")
    _listed[(ctx.settings["url"], ctx.settings["key"])] = (time.monotonic(), ids)
    return ids


def _http(ctx, method: str, path: str, body: dict | None = None, raw: bool = False):
    key = ctx.settings["key"]
    if not key:
        raise KitError("speech-elevenlabs.key is not set (the vault name of an API key)")
    url = str(ctx.settings["url"]).rstrip("/")
    host = urllib.parse.urlsplit(url)
    if host.scheme != "https" and host.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise KitError("the ElevenLabs address must be https")
    req = urllib.request.Request(url + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json", "xi-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=parse_duration(ctx.settings["timeout"])) as r:
            data = r.read()
    except urllib.error.HTTPError as e:
        raise KitError(f"ElevenLabs: HTTP {e.code}: {_error(e)}".replace(key, "***"),
                       retry=e.code == 429 or e.code >= 500) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise KitError(f"ElevenLabs: {getattr(e, 'reason', None) or type(e).__name__}", retry=True) from None
    if raw:
        return data
    try:
        return json.loads(data or b"{}")
    except ValueError:
        raise KitError("ElevenLabs: the answer is not JSON") from None


def _error(e: urllib.error.HTTPError) -> str:
    try:
        detail = json.loads(e.read() or b"{}").get("detail")
    except ValueError:
        return e.reason or ""
    if isinstance(detail, dict):
        detail = detail.get("message") or detail.get("status")
    return str(detail or e.reason or "")[:200]
