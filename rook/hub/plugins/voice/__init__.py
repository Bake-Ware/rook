"""``voice.*``: talk to your agents from phones, the desktop app and a browser.

The voice service (``services/voice``: speech recognition, the spoken replies
and the voice agent) used to be its own server with its own address. This hub
plugin puts it on the hub site, served by ``rook hub serve``:

* ``/voice/ws`` (the WebSocket the Android and desktop apps talk to),
  ``/voice/api/transcribe``, ``/voice/api/voice``, ``/voice/voices``,
  ``/voice/modes``, ``/voice/health``, the browser client at ``/voice/`` and
  voice key admin at ``/voice/admin``. ``/api/voice``, ``/api/transcribe`` and
  ``/voices`` answer at the site root too (``voice.root_aliases``): the
  Android app builds those paths from the host alone.
* where the speech runs is ``voice.engine``: ``here`` (in the hub process;
  needs ``pip install 'rook[voice]'`` and the model files, so a small hub that
  does not use voice never loads any of it) or ``remote`` (forward everything
  to a standalone voice server on a machine with a GPU, ``voice.remote_url``).

Without what the chosen engine needs, the plugin does not load and the
Plugins page says what is missing in plain words. Its settings (the former
``voice`` service settings, same keys and variables) are on Settings > Voice;
the Voice page (Manage > Voice) shows its state and has a microphone check.

The standalone server keeps working unchanged (``python -m
services.voice.server``); see docs/operations/voice.md for the move.
"""

from __future__ import annotations

import base64
import logging
import time
from pathlib import Path
from typing import Any

from ....core.plugin import Plugin, capability, place, setting
from . import engine as eng

log = logging.getLogger("rook.hub.plugins.voice")

PREFIX = "/voice"
#: Root paths the Android app uses (it keeps only the host of the voice URL).
ALIASES = ("/api/voice", "/api/transcribe", "/voices")
OWNER = "plugin:voice"
#: Settings the hub keeps for itself, never handed to the voice service.
HUB_ONLY = frozenset({"engine", "remote_url", "remote_verify_tls", "root_aliases"})
#: Longest microphone check the Voice page sends (16 kHz mono 16-bit).
MAX_CHECK_SECONDS = 15


def _s(name, type_, default=None, **kw):
    return setting(name, type_, default, **kw)


SETTINGS = (
    # Where it runs (the hub's own; new with the plugin).
    _s("engine", str, "here", choices=("here", "remote"), apply="reload", group="Engine",
       order=1, label="Where speech runs",
       help="here: in the hub process (needs pip install 'rook[voice]' and the model files). "
            "remote: forward to a voice server on another machine (the address below)."),
    _s("remote_url", "url", "", apply="reload", group="Engine", order=2,
       label="Voice server address",
       help="For remote: the standalone voice server, e.g. https://gpu.example.com:8900. "
            "Phones and apps keep using this hub's address."),
    _s("remote_verify_tls", bool, True, apply="reload", group="Engine", order=3, advanced=True,
       label="Check the voice server's certificate",
       help="Off only for a self-signed certificate on a private network."),
    _s("model_dir", "path", "", env="VOICE_MODEL_DIR", apply="reload", group="Engine", order=4,
       label="Model folder",
       help="For here: kokoro-v1.0.onnx, voices-v1.0.bin and smart-turn-v3.2-cpu.onnx, and the "
            "voice keys and history databases. Blank: the plugin's data folder on this hub."),
    _s("root_aliases", bool, True, apply="reload", group="Engine", order=5, advanced=True,
       label="Also answer the Android app's root paths",
       help="/api/voice, /api/transcribe and /voices at the site root, as well as under "
            "/voice/. The Android app builds those from the host alone."),
    # Access (the former voice service settings; same keys and variables).
    _s("token", "str", secret=True, env="VOICE_TOKEN", apply="reload", group="Access",
       label="Client bearer token",
       help="An owner voice key. In remote mode the hub also uses it for the Voice page's "
            "checks, so the voice server must accept it."),
    _s("allow_anonymous", bool, False, env="VOICE_ALLOW_ANONYMOUS", apply="reload",
       group="Access", label="Allow clients without a token",
       help="Keyless clients get conversation and web search only."),
    _s("bind", "str", "127.0.0.1", env="VOICE_BIND", bootstrap=True, apply="restart",
       group="Standalone server", advanced=True, label="Listen address",
       help="Only for the standalone server (python -m services.voice.server)."),
    _s("port", int, 8900, env="VOICE_PORT", bootstrap=True, apply="restart", min=1, max=65535,
       group="Standalone server", advanced=True, label="Port",
       help="Only for the standalone server."),
    # Recognition
    _s("whisper_model", "str", "small.en", env="WHISPER_MODEL", apply="reload",
       label="Speech recognition model", group="Recognition"),
    _s("whisper_device", "str", "cpu", env="WHISPER_DEVICE", apply="reload",
       label="Recognition device", group="Recognition"),
    _s("whisper_compute", "str", "int8", env="WHISPER_COMPUTE", apply="reload",
       label="Compute type", group="Recognition"),
    _s("min_speech_ms", int, 450, env="MIN_SPEECH_MS", min=0, apply="reload",
       label="Minimum speech (ms)", group="Recognition", advanced=True),
    _s("min_rms", float, 0.008, env="MIN_RMS", min=0.0, apply="reload", label="Energy floor",
       group="Recognition", advanced=True),
    _s("max_no_speech", float, 0.6, env="MAX_NO_SPEECH", min=0.0, max=1.0, apply="reload",
       label="No-speech ceiling", group="Recognition", advanced=True),
    _s("min_logprob", float, -1.0, env="MIN_LOGPROB", apply="reload",
       label="Minimum log-probability", group="Recognition", advanced=True),
    # Speech
    _s("default_voice", "str", "af_heart", env="VOICE", overridable=("user",), apply="reload",
       label="Voice", group="Speech"),
    _s("assistant_name", "str", "", env="ROOK_VOICE_ASSISTANT_NAME", overridable=("user",),
       pattern=r"^.{0,60}$", apply="reload", label="Assistant name", group="Speech",
       help="Blank: the name of the persona assigned to family 'voice' (persona plugin), "
            "else 'Rook'."),
    _s("owner", "str", "", env="ROOK_VOICE_OWNER", overridable=("user",),
       pattern=r"^.{0,60}$", apply="reload", label="Whose assistant", group="Speech",
       help="Name in 'your personal voice assistant'. Blank: the voice persona's owner, "
            "else neutral wording."),
    _s("show_thinking", bool, False, scope="user", label="Show thinking", group="Speech"),
    _s("hotword_enabled", bool, True, scope="user", label="Wake word on this account",
       group="Speech"),
    # Agent
    _s("llm_url", "url", "http://127.0.0.1:1234/v1/chat/completions", env="VLLM_URL",
       apply="reload", label="Language model URL", group="Agent"),
    _s("llm_model", "str", "", env="VLLM_MODEL", apply="reload", label="Model id",
       group="Agent"),
    _s("mcp_url", "url", "http://127.0.0.1:8765/mcp", env="ROOK_MCP_URL", apply="reload",
       label="Rook MCP URL", group="Agent"),
    _s("mcp_token", "str", secret=True, env="ROOK_MCP_TOKEN", apply="reload",
       label="Rook MCP token", group="Agent"),
    _s("acp_host", "str", "127.0.0.1", env="ACP_HOST", apply="reload", label="ACP host",
       group="Agent"),
    _s("acp_port", int, 9200, env="ACP_PORT", min=1, max=65535, apply="reload",
       label="ACP port", group="Agent"),
    _s("acp_auto_approve", bool, True, env="ACP_AUTO_APPROVE", apply="reload",
       label="Auto-approve ACP prompts", group="Agent",
       help="Unattended tool permission for the voice agent."),
    _s("direct_tool_budget", int, 1, env="DIRECT_TOOL_BUDGET", min=0, apply="reload",
       label="Direct tool calls per turn", group="Agent", advanced=True),
)


def _env_value(value: Any) -> str:
    if value is True:
        return "1"
    if value is False:
        return "0"
    return str(value)


NOT_SERVED = ("Voice on the hub's address needs the single-process hub: run it with "
              "`rook hub serve`. This hub runs as two processes (rook-dashboard and "
              "rook-mcp), where nothing serves /voice/; keep using the standalone voice "
              "server (python -m services.voice.server) until you switch.")


def requirements(engine: str, remote_url: str, model_dir: str,
                 packages: tuple = eng.ENGINE_PACKAGES, *, served: bool | None = None) -> dict:
    """What the chosen engine still needs, for available() and the Voice
    page (also when the plugin is not loaded). ``reason`` is empty when
    nothing is missing. ``served`` is whether this process serves hub site
    paths (default: :func:`rook.hub.site.serving`)."""
    from ...site import serving
    out: dict = {"engine": engine, "extras_missing": [], "models_missing": [],
                 "engine_code": None, "model_dir": model_dir, "reason": "",
                 "served": serving() if served is None else bool(served)}
    if not out["served"]:
        # Checked first: without rook hub serve nothing would reach the
        # engine, so never load the speech models for nothing.
        out["reason"] = NOT_SERVED
        return out
    if engine == "remote":
        if not remote_url:
            out["reason"] = ("Voice is set to run on another machine, but its address is not "
                             "set: Settings > Voice > Voice server address.")
        elif eng.has_userinfo(remote_url):
            out["reason"] = ("The voice server address must not carry a user name or "
                             "password: voice keys go in Client bearer token.")
        return out
    out["engine_code"] = eng.find_engine(packages)
    out["extras_missing"] = eng.missing_extras()
    if out["engine_code"] is None:
        out["reason"] = ("The voice service code is not installed with this hub. Run the hub "
                         "from a Rook checkout, or install a release that includes it.")
    elif out["extras_missing"]:
        out["reason"] = (f"Needs the voice extras to run speech on this hub: {eng.EXTRAS_HINT} "
                         f"(missing {', '.join(out['extras_missing'])}). Or run voice on "
                         "another machine: Settings > Voice > Where speech runs: remote.")
    else:
        out["models_missing"] = eng.missing_models(model_dir)
        if out["models_missing"]:
            out["reason"] = (f"Speech model files are missing from {model_dir}: "
                             f"{', '.join(out['models_missing'])} (see services/voice/README.md).")
    return out


class Voice(Plugin):
    """Voice: talk to your agents from phones, the desktop app and a browser, through the hub."""

    NAMESPACE = "voice"
    NAME = "voice"
    CORE_API = ">=1.1,<2"
    PLACEMENT = place("is_hub", run="one")
    SETTINGS = SETTINGS
    SKILL = ("### voice\n"
             "`rook_call(worker=\"rook\", cap=\"voice.status\")` says whether voice is served by "
             "this hub (and where speech runs). Speaking on a phone is `voice.speak` on that "
             "phone's worker, not this.\n")
    #: Where to look for the voice service (tests point it at a fake one).
    ENGINE_PACKAGES = eng.ENGINE_PACKAGES

    def __init__(self) -> None:
        super().__init__()
        self._node: Any = None
        self.engine: Any = None
        self._why = ""
        self._mounted: list[str] = []
        self.last_check: dict | None = None

    # -- wiring ------------------------------------------------------------------
    def bind_host(self, node) -> None:
        self._node = node

    def model_dir(self) -> str:
        return str(self.settings.get("model_dir") or "") or str(self.data_dir)

    def requirements(self) -> dict:
        return requirements(self.settings.get("engine") or "here",
                            str(self.settings.get("remote_url") or ""), self.model_dir(),
                            self.ENGINE_PACKAGES)

    def available(self) -> bool:
        self._why = self.requirements()["reason"]
        return not self._why

    def unavailable_reason(self) -> str:
        return self._why

    def engine_env(self) -> dict[str, str]:
        """The voice service's environment from this plugin's settings: values
        an operator stored (or put in the vault) only; defaults stay the
        service's own, and the hub's environment always wins."""
        env: dict[str, str] = {}
        for s in self.SETTINGS:
            if not s.env or s.name in HUB_ONLY or s.scope != "hub":
                continue
            if self.settings.source(s.name) not in ("stored", "vault"):
                continue
            value = self.settings.get(s.name)
            if value is None or value == "":
                continue
            env[s.env] = _env_value(value)
        env["VOICE_MODEL_DIR"] = self.model_dir()
        return env

    def _token(self) -> str | None:
        return self.settings.get("token") or None

    async def start(self) -> None:
        if (self.settings.get("engine") or "here") == "remote":
            self.engine = eng.RemoteEngine(str(self.settings.get("remote_url")), PREFIX,
                                           verify_tls=bool(self.settings.get("remote_verify_tls")),
                                           token=self._token())
        else:
            pkg = eng.find_engine(self.ENGINE_PACKAGES)
            self.engine = eng.LocalEngine(pkg, self.engine_env(), PREFIX)
        self.engine.start()
        self._mount()

    async def stop(self) -> None:
        site = getattr(self._node, "site", None)
        if site is not None:
            site.unmount(OWNER)
        self._mounted = []
        engine, self.engine = self.engine, None
        if engine is not None:
            await engine.stop()

    def _mount(self) -> None:
        site = getattr(self._node, "site", None)
        if site is None:
            return
        self._mounted = [site.mount(OWNER, PREFIX, self.serve)]
        if self.settings.get("root_aliases"):
            for path in ALIASES:
                try:
                    self._mounted.append(site.alias(OWNER, path, self.serve_alias))
                except ValueError as e:
                    log.warning("voice: not serving %s: %s", path, e)

    # -- the site ------------------------------------------------------------------
    async def serve(self, request):
        from aiohttp import web
        if request.path == PREFIX:
            # The browser client's links are relative: it must load from /voice/.
            q = request.rel_url.query_string
            raise web.HTTPFound(PREFIX + "/" + (("?" + q) if q else ""))
        engine = self.engine
        if engine is None:
            return web.json_response({"error": "Voice is off on this hub."}, status=503)
        return await engine.handle(request)

    async def serve_alias(self, request):
        from aiohttp import web
        engine = self.engine
        if engine is None:
            return web.json_response({"error": "Voice is off on this hub."}, status=503)
        return await engine.handle(request, alias=True)

    # -- status ----------------------------------------------------------------------
    def status(self) -> dict:
        e = self.engine
        kind = self.settings.get("engine") or "here"
        out = {"loaded": True, "engine": kind, "state": getattr(e, "state", "stopped"),
               "error": getattr(e, "error", ""), "paths": list(self._mounted),
               "served": bool(self._mounted)}
        if kind == "remote" and e is not None:
            out["remote_host"] = e.host
        if kind == "here" and e is not None:
            out["started_at"] = e.started_at or None
            out["ready_at"] = e.ready_at or None
        if self.last_check is not None:
            out["last_check"] = self.last_check
        return out

    @capability("status", risk="read", fields="*")
    def status_cap(self) -> dict:
        """voice.status — whether this hub serves voice (/voice/ws), where speech runs and whether it is ready."""
        s = self.status()
        # Band readers get the state, not hosts or errors in full.
        return {"served": s["served"], "engine": s["engine"], "state": s["state"],
                "paths": s["paths"]}

    async def check(self) -> dict:
        e = self.engine
        if e is None:
            return {"ok": False, "state": "stopped", "error": "Voice is off on this hub."}
        res = await e.health()
        res["checked_at"] = time.time()
        self.last_check = res
        return res

    async def transcribe(self, pcm: bytes) -> dict:
        if not pcm or len(pcm) % 2 or len(pcm) > MAX_CHECK_SECONDS * 32000:
            raise ValueError(f"send 16 kHz mono 16-bit audio, up to {MAX_CHECK_SECONDS} s")
        if self.engine is None:
            raise ValueError("Voice is off on this hub.")
        t0 = time.monotonic()
        try:
            text = await self.engine.transcribe(pcm)
        except (RuntimeError, OSError) as e:
            raise ValueError(f"Transcription failed: {e}") from None
        except Exception as e:  # noqa: BLE001 - the page shows one line
            log.exception("voice: microphone check failed")
            raise ValueError(f"Transcription failed: {type(e).__name__}") from None
        return {"ok": True, "text": text, "seconds": round(len(pcm) / 32000, 2),
                "latency_ms": round((time.monotonic() - t0) * 1000)}

    # -- Manage > Voice ------------------------------------------------------------------
    def page(self, svc) -> dict:
        return page_data(svc, self._node, self)

    async def page_action(self, svc, data: dict, actor: str) -> dict:
        action = data.get("action")
        if action == "voice_check":
            return {"ok": True, "check": await self.check(), "status": self.status()}
        if action == "voice_transcribe":
            raw = str(data.get("pcm") or "")
            try:
                pcm = base64.b64decode(raw, validate=True)
            except (ValueError, TypeError):
                raise ValueError("pcm must be base64") from None
            return await self.transcribe(pcm)
        raise ValueError("Use voice_check or voice_transcribe")


def page_data(svc, node, plugin: "Voice | None") -> dict:
    """What Manage > Voice shows. Works whether or not the plugin loaded (when
    it did not, the reason is the point of the page)."""
    def val(key: str) -> Any:
        try:
            return svc.resolve(f"voice.{key}").get("value")
        except Exception:  # noqa: BLE001
            return None

    keys = ("engine", "remote_url", "remote_verify_tls", "model_dir", "root_aliases",
            "allow_anonymous", "default_voice", "assistant_name", "whisper_model",
            "whisper_device", "llm_url", "llm_model")
    settings = {}
    for k in keys:
        try:
            r = svc.resolve(f"voice.{k}")
        except Exception:  # noqa: BLE001
            continue
        settings[k] = {"value": r.get("value"), "locked": bool(r.get("locked")),
                       "env": r.get("env"), "source": r.get("source")}
    if plugin is not None:
        model_dir = plugin.model_dir()
        status = plugin.status()
        packages = plugin.ENGINE_PACKAGES
    else:
        base = getattr(node, "_state_dir", None)
        model_dir = str(val("model_dir") or "") or (
            str(Path(base) / "plugins" / "voice") if base else "")
        st = (getattr(getattr(node, "host", None), "status", {}) or {}).get("voice") or {}
        status = {"loaded": False, "state": st.get("state") or "unavailable",
                  "reason": st.get("reason") or "", "served": False, "paths": [],
                  "engine": val("engine") or "here"}
        packages = eng.ENGINE_PACKAGES
    req = requirements(str(val("engine") or "here"), str(val("remote_url") or ""), model_dir,
                       packages)
    token_set = False
    try:
        token_set = bool(svc.resolve("voice.token").get("value"))
    except Exception:  # noqa: BLE001
        pass
    return {"status": status, "requirements": req, "settings": settings,
            "token_set": token_set, "prefix": PREFIX, "aliases": list(ALIASES),
            "extras_hint": eng.EXTRAS_HINT, "model_files": list(eng.MODEL_FILES),
            "max_check_seconds": MAX_CHECK_SECONDS}


PLUGIN = Voice
