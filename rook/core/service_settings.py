"""Settings schemas of the services that run beside the hub and fetch their
configuration from it with a scoped token (``settings.fetch``): the voice
service (``services/voice``) and the decision engine client it hosts.

Stdlib-only, so both sides import the same declaration: the hub
(:mod:`rook.hub.settings_schema`, to store, validate and show them) and the
service itself (:mod:`services.voice.config`, to resolve them). A setting the
service reads is declared here, and a setting declared here is read by the
service (``tests/test_voice_settings.py`` cross-checks both directions).

Resolution in the service: its own environment wins, then the hub's stored
value, then the last-known-good copy cached on disk (non-secret values only),
then the default below. ``bootstrap`` settings are needed to reach the hub at
all (its address and token) or to open the listening socket, so they come
from the environment only.

Every setting accepts the canonical ``ROOK_<NAMESPACE>_<NAME>`` variable in
addition to its legacy name (``WHISPER_MODEL`` and
``ROOK_VOICE_WHISPER_MODEL`` both work; the legacy name is listed first).
"""

from __future__ import annotations

from typing import Any

from .plugin import Setting, canonical_env, setting


def _with_canonical(namespace: str, name: str, env: Any) -> tuple:
    names = [env] if isinstance(env, str) else list(env or ())
    canon = canonical_env(f"{namespace}.{name}")
    if canon not in names:
        names.append(canon)
    return tuple(n for n in names if n)


def _v(name: str, type_: Any = str, default: Any = None, *, env: Any = None,
       **kw: Any) -> Setting:
    return setting(name, type_, default, env=_with_canonical("voice", name, env), **kw)


def _d(name: str, type_: Any = str, default: Any = None, *, env: Any = None,
       **kw: Any) -> Setting:
    return setting(name, type_, default, env=_with_canonical("decision", name, env), **kw)


#: Settings the voice server does not read itself: they are the Android app's
#: per-user preferences (docs/design/settings.md 1.12), stored on the hub so an
#: account keeps them across devices. ``settings.fetch("voice")`` still returns
#: them under ``users``.
VOICE_CLIENT_SETTINGS = frozenset({"show_thinking", "hotword_enabled"})

VOICE: list[Setting] = [
    # Access
    _v("bind", "str", "127.0.0.1", env="VOICE_BIND", bootstrap=True, apply="restart",
       label="Listen address", group="Access"),
    _v("port", int, 8900, env="VOICE_PORT", bootstrap=True, apply="restart", min=1,
       max=65535, label="Port", group="Access"),
    _v("tls_cert", "path", "", env="VOICE_TLS_CERT", apply="restart",
       label="TLS certificate (PEM path)", group="Access",
       help="Blank: plain HTTP, e.g. behind a TLS proxy. A path on the voice host."),
    _v("tls_key", "path", "", env="VOICE_TLS_KEY", apply="restart",
       label="TLS key (PEM path)", group="Access"),
    _v("token", "str", secret=True, env="VOICE_TOKEN", apply="restart",
       label="Client bearer token", group="Access"),
    _v("allow_anonymous", bool, False, env="VOICE_ALLOW_ANONYMOUS", apply="restart",
       label="Allow clients without a token", group="Access",
       help="Only when no client token is set; for trusted LANs."),
    # Persona (the persona plugin maps an assistant persona onto these two)
    _v("assistant_name", "str", "Rook", env="ROOK_VOICE_ASSISTANT_NAME",
       label="Assistant name", group="Persona",
       help="The name the assistant introduces itself with."),
    _v("owner", "str", "", env="ROOK_VOICE_OWNER", label="Owner name", group="Persona",
       help="Used as \"<owner>'s personal voice assistant\"; blank uses neutral phrasing."),
    # Providers
    _v("stt_provider", "str", "faster-whisper", choices=("faster-whisper",), apply="restart",
       label="Speech recognition provider", group="Providers"),
    _v("tts_provider", "str", "kokoro", choices=("kokoro",), apply="restart",
       label="Speech synthesis provider", group="Providers"),
    _v("llm_provider", "str", "openai-compatible", choices=("openai-compatible",),
       label="Language model API", group="Providers",
       help="Chat-completions API with tool calls (vLLM, llama.cpp, LM Studio, ...)."),
    _v("turn_provider", "str", "smart-turn", choices=("smart-turn",), apply="restart",
       label="Turn detection provider", group="Providers"),
    # Recognition
    _v("whisper_model", "str", "small.en", env="WHISPER_MODEL", apply="restart",
       label="Speech recognition model", group="Recognition"),
    _v("whisper_device", "str", "cpu", env="WHISPER_DEVICE", apply="restart",
       label="Recognition device", group="Recognition"),
    _v("whisper_compute", "str", "int8", env="WHISPER_COMPUTE", apply="restart",
       label="Compute type", group="Recognition"),
    _v("stt_language", "str", "en", label="Recognition language", group="Recognition"),
    _v("min_speech_ms", int, 200, env="MIN_SPEECH_MS", min=20, max=5000,
       label="Minimum speech (ms)", group="Recognition", advanced=True,
       help="Shorter utterances are dropped as noise."),
    _v("min_rms", float, 0.008, env="MIN_RMS", min=0.0, label="Energy floor",
       group="Recognition", advanced=True),
    _v("max_no_speech", float, 0.6, env="MAX_NO_SPEECH", min=0.0, max=1.0,
       label="No-speech ceiling", group="Recognition", advanced=True),
    _v("min_logprob", float, -1.0, env="MIN_LOGPROB", label="Minimum log-probability",
       group="Recognition", advanced=True),
    # Turn taking
    _v("turn_model", "str", "smart-turn-v3.2-cpu.onnx", apply="restart",
       label="Turn model file", group="Turn taking", advanced=True,
       help="File name in the model directory."),
    _v("turn_check_ms", int, 400, min=20, max=5000, label="Check for end of turn after (ms)",
       group="Turn taking", help="Silence before the turn model is asked."),
    _v("turn_silence_ms", int, 600, min=20, max=10000,
       label="End turn after silence (ms) when complete", group="Turn taking"),
    _v("turn_max_silence_ms", int, 2500, min=100, max=30000,
       label="End turn after silence (ms) regardless", group="Turn taking"),
    _v("max_utterance_s", int, 30, min=1, max=120, label="Longest utterance (s)",
       group="Turn taking", advanced=True),
    # Speech
    _v("tts_model", "str", "kokoro-v1.0.onnx", apply="restart", label="Synthesis model file",
       group="Speech", advanced=True, help="File name in the model directory."),
    _v("tts_voices", "str", "voices-v1.0.bin", apply="restart", label="Voices file",
       group="Speech", advanced=True),
    _v("default_voice", "str", "af_heart", env="VOICE", overridable=("user",),
       label="Voice", group="Speech"),
    _v("tts_speed", float, 1.0, min=0.5, max=2.0, label="Speaking rate", group="Speech"),
    _v("tts_language", "str", "en-us", label="Synthesis language", group="Speech"),
    _v("show_thinking", bool, False, scope="user", label="Show thinking", group="Speech",
       help="Read by the app: opts into decision events."),
    _v("hotword_enabled", bool, True, scope="user", label="Wake word on this account",
       group="Wake word", help="Read by the app."),
    # Wake word (detected on the device; the server advertises these in its
    # session event, clients that do not know them ignore them)
    _v("wake_model", "str", "", label="Wake word model", group="Wake word",
       help="Model name advertised to clients; blank keeps each app's built-in choice."),
    _v("wake_threshold", float, 0.5, min=0.05, max=0.99, label="Wake word threshold",
       group="Wake word"),
    # Agent
    _v("llm_url", "url", "http://127.0.0.1:1234/v1/chat/completions", env="VLLM_URL",
       label="Language model URL", group="Agent"),
    _v("llm_model", "str", "qwopus3.6-35b-a3b-v1-mtp", env="VLLM_MODEL", label="Model id",
       group="Agent"),
    _v("llm_api_key", "str", secret=True, env="VLLM_API_KEY", label="Language model API key",
       group="Agent", help="Sent as a bearer token when set."),
    _v("acp_host", "str", "127.0.0.1", env="ACP_HOST", label="ACP host", group="Agent"),
    _v("acp_port", int, 9200, env="ACP_PORT", min=1, max=65535, label="ACP port",
       group="Agent"),
    _v("acp_auto_approve", bool, True, env="ACP_AUTO_APPROVE",
       label="Auto-approve ACP prompts", group="Agent",
       help="Unattended tool permission for the voice agent."),
    # Timing (latency budgets)
    _v("stt_timeout_s", float, 25.0, min=1.0, max=300.0, label="Recognition budget (s)",
       group="Timing"),
    _v("plan_timeout_s", float, 25.0, min=1.0, max=300.0, label="Planning request budget (s)",
       group="Timing", help="One language model request choosing a reply or tool."),
    _v("reply_timeout_s", float, 60.0, min=1.0, max=600.0, label="Reply budget (s)",
       group="Timing", help="The whole model step of a turn, retries included."),
    _v("tts_timeout_s", float, 25.0, min=1.0, max=300.0, label="Synthesis budget (s)",
       group="Timing"),
    _v("turn_detect_timeout_s", float, 2.0, min=0.1, max=30.0,
       label="Turn detection budget (s)", group="Timing", advanced=True),
    _v("model_wait_s", float, 5.0, min=0.1, max=60.0, label="Model queue wait (s)",
       group="Timing", advanced=True, help="How long a request waits for a busy model."),
    _v("read_tool_timeout_s", float, 45.0, min=1.0, max=600.0,
       label="Read-only tool budget (s)", group="Timing"),
    _v("agent_timeout_s", float, 600.0, min=10.0, max=7200.0,
       label="Delegated agent budget (s)", group="Timing"),
    # Hub link (bootstrap: needed to reach the hub at all)
    _v("mcp_url", "url", "http://127.0.0.1:8765/mcp", env="ROOK_MCP_URL", bootstrap=True,
       label="Rook MCP URL", group="Hub link"),
    _v("mcp_token", "str", secret=True, env="ROOK_MCP_TOKEN", bootstrap=True,
       label="Rook MCP token", group="Hub link",
       help="List this token's label or agent_id in core.settings.service_readers.voice "
            "so the service can fetch these settings."),
    _v("settings_refresh_s", int, 300, min=0, max=86400, label="Settings refresh (s)",
       group="Hub link", help="How often voice re-reads these settings; 0: at start and "
                              "on SIGHUP only."),
    _v("settings_cache", "path", "", env="VOICE_SETTINGS_CACHE", bootstrap=True,
       label="Settings cache file", group="Hub link", advanced=True,
       help="Last-known-good settings (no secrets) for starts while the hub is "
            "unreachable. Blank: voice-settings.json in the model directory."),
    # Storage
    _v("model_dir", "path", "", env="VOICE_MODEL_DIR", bootstrap=True, apply="restart",
       label="Model directory", group="Storage",
       help="Holds the model files and static/. Blank: the working directory "
            "(models: the service's own directory)."),
    _v("state_db", "path", "", env="VOICE_STATE_DB", apply="restart",
       label="State database", group="Storage", advanced=True,
       help="Blank: voice-state.sqlite3 in the model directory."),
]

#: The decision engine client runs inside the voice process (shadow mode:
#: it observes turns and never changes what the assistant does). Its client
#: code is on the ``feature/voice-decision-shadow`` branch; it resolves these
#: with ``services.voice.config.ServiceConfig("decision", DECISION)``.
DECISION: list[Setting] = [
    _d("url", "str", "", env="DECISION_URL", label="Engine endpoint", group="General",
       help="Blank turns the engine off."),
    _d("mode", "str", "shadow", choices=("off", "shadow"), label="Mode", group="General",
       help="shadow: score each turn and show it to users who opted in; never act on it."),
    _d("assistant_names", list, ["rook", "assistant"], env="DECISION_ASSISTANT_NAMES",
       label="Assistant names", group="General"),
    _d("timeout_ms", int, 150, env="DECISION_TIMEOUT_MS", min=10, max=5000,
       label="Per-turn timeout (ms)", group="Timing"),
    _d("recent_speech_seconds", int, 15, env="DECISION_RECENT_SPEECH_SECONDS", min=1,
       label="Recent speech window (s)", group="Timing"),
    _d("silence_seconds", int, 15, env="DECISION_SILENCE_SECONDS", min=1,
       label="Silence window (s)", group="Timing"),
    _d("raw_retention_days", int, 30, env="DECISION_RAW_RETENTION_DAYS", min=0,
       label="Keep raw inputs (days)", group="Data"),
]
