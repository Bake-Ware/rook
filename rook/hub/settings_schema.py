"""Every setting the hub knows about, keyed ``<namespace>.<name>``.

Sources, in the order they are merged:

* ``core``: the hub, band and worker core (declared here, from the inventory in
  docs/design/settings.md part 1, with today's variable names as env aliases);
* hub-placed plugins (their ``SETTINGS``, from the hub node's plugin host);
* worker plugins (``SETTINGS`` of every Plugin class under
  ``rook.worker.plugins``, whether or not it loads on the hub);
* services that run beside the hub and fetch their settings with a scoped
  token (voice, decision engine): declared here so the hub can store and
  serve them without importing the service.

Each entry also records which process reads it (``owner``): ``dashboard``,
``mcp``, ``watchdog``, ``worker`` or ``service:<name>``. The owner's
environment is what locks a key; the MCP server reads its own, the dashboard
reports its own through the store's ``runtime`` table.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
from dataclasses import dataclass
from typing import Any

from ..core.plugin import RISKS, Plugin, Setting, setting

log = logging.getLogger("rook.hub.settings_schema")


@dataclass(frozen=True)
class Entry:
    key: str
    namespace: str
    setting: Setting
    owner: str                  # dashboard | mcp | watchdog | worker | service:<name>
    origin: str                 # core | hub-plugin | worker-plugin | service
    plugin: str = ""            # module / service name for plugin pages
    file_key: str | None = None     # setup.json field that still feeds it (legacy)
    config_key: str | None = None   # worker config.json key it is delivered as
    managed: str | None = None      # stored elsewhere (e.g. "enrollment"): shown, not set here

    def env_names(self) -> tuple:
        """Variables the owner actually reads. Only hub plugins resolve through
        SettingsView, which also accepts the canonical ``ROOK_<NS>_<NAME>``."""
        return self.setting.env_names(self.namespace if self.origin == "hub-plugin" else "")

    def describe(self) -> dict:
        d = self.setting.describe()
        d.update(key=self.key, namespace=self.namespace, owner=self.owner, origin=self.origin,
                 env_names=list(self.env_names()))
        if self.plugin:
            d["plugin"] = self.plugin
        if self.file_key:
            d["file"] = f"setup.json:{self.file_key}"
        if self.managed:
            d["managed"] = self.managed
        return d


def _s(name: str, type_: Any = str, default: Any = None, **kw: Any) -> Setting:
    return setting(name, type_, default, **kw)


# -- core -------------------------------------------------------------------
# (setting, owner, extra) - keep in the order the Hub page shows them.

_CORE: list[tuple[Setting, str, dict]] = [
    # General
    (_s("hub.domain", "str", "hub.example.com", env="ROOK_DOMAIN", flag="--domain",
        label="Public dashboard host", group="General", order=1,
        help="host[:port] of this dashboard, used in installer and update URLs."),
     "dashboard", {"file_key": "pyz_domain"}),
    (_s("hub.public_relay", "hostport", "hub.example.com:443", env="ROOK_HUB_PUBLIC",
        flag="--hub-public", label="Relay address workers dial", group="General", order=2,
        help="host:port templated into installers and new bands."),
     "dashboard", {"file_key": "hub_public"}),
    (_s("hub.band_name", "str", "rook-band", env="ROOK_BAND_NAME", flag="--band-name",
        label="Primary band label", group="General", order=3,
        help="Cosmetic name of the primary band in the dashboard and installers."),
     "dashboard", {"file_key": "band_name"}),
    # Network (bootstrap: read before the store is reachable)
    (_s("dashboard.bind", "str", "0.0.0.0", env="ROOK_BIND", flag="--bind", bootstrap=True,
        apply="restart", label="Dashboard listen address", group="Network"), "dashboard", {}),
    (_s("dashboard.port", int, 7005, env="ROOK_PORT", flag="--port", bootstrap=True,
        apply="restart", min=1, max=65535, label="Dashboard port", group="Network"),
     "dashboard", {}),
    (_s("dashboard.relay_host", "str", "127.0.0.1", env="ROOK_HUB_HOST", flag="--hub-host",
        bootstrap=True, apply="restart", label="Relay host (dashboard)", group="Network"),
     "dashboard", {}),
    (_s("dashboard.relay_port", int, 7474, env="ROOK_HUB_PORT", flag="--hub-port",
        bootstrap=True, apply="restart", min=1, max=65535, label="Relay port (dashboard)",
        group="Network"), "dashboard", {}),
    (_s("mcp.listen", "hostport", "127.0.0.1:8765", env="ROOK_MCP_BIND", flag="--bind",
        bootstrap=True, apply="restart", label="MCP listen address", group="Network"),
     "mcp", {}),
    (_s("mcp.relay", "hostport", "127.0.0.1:7474", env="ROOK_HUB", flag="--hub",
        bootstrap=True, apply="restart", label="Relay address (MCP)", group="Network"),
     "mcp", {}),
    (_s("mcp.public_url", "str", "", env="ROOK_MCP_PUBLIC_URL", flag="--public-url",
        bootstrap=True, apply="restart", label="Public MCP URL", group="Network",
        help="Enables the OAuth front door for web connectors."), "mcp", {}),
    (_s("mcp.allowed_hosts", list, [], env="ROOK_ALLOWED_HOSTS", flag="--allowed-hosts",
        bootstrap=True, apply="restart", label="Allowed Host headers", group="Network"),
     "mcp", {}),
    # Sign-in
    (_s("auth.web_user", "str", "", env="ROOK_WEB_USER", flag="--web-user", bootstrap=True,
        apply="restart", label="Operator account name", group="Sign-in"), "dashboard", {}),
    (_s("auth.web_pass", "str", secret=True, env="ROOK_WEB_PASS", flag="--web-pass",
        bootstrap=True, apply="restart", label="Operator bootstrap password", group="Sign-in",
        help="Creates or updates the operator account at start."), "dashboard", {}),
    (_s("mcp.admin_password", "str", secret=True, env="ROOK_MCP_AUTH_PASSWORD",
        flag="--admin-password", bootstrap=True, apply="restart",
        label="MCP /tokens page password", group="Sign-in",
        deprecated="use the dashboard API tokens page"), "mcp", {}),
    (_s("mcp.static_token", "str", secret=True, env="ROOK_MCP_STATIC_TOKEN",
        flag="--static-token", bootstrap=True, apply="restart", label="Shared static token",
        group="Sign-in", help="Kept for compatibility; mint per-agent tokens instead."),
     "mcp", {}),
    (_s("auth.google_client_file", "path", "", env="ROOK_GOOGLE_CLIENT_FILE", bootstrap=True,
        apply="restart", label="Google OAuth client file", group="Sign-in", advanced=True),
     "dashboard", {}),
    (_s("auth.google_web_login", bool, True, env="ROOK_GOOGLE_WEB_LOGIN", bootstrap=True,
        apply="restart", label="Google sign-in on the web", group="Sign-in", advanced=True),
     "dashboard", {}),
    # Updates
    (_s("updates.push", bool, True, env="ROOK_PUSH_UPDATES", bootstrap=True, apply="restart",
        label="Push updates to outdated workers", group="Updates"), "dashboard", {}),
    (_s("updates.apk_sha256", list, [], env="ROOK_PUBLIC_APK_SHA256", bootstrap=True,
        label="Published APK hashes", group="Updates", advanced=True), "dashboard", {}),
    (_s("updates.signing_key", "path", "", env="ROOK_UPDATE_KEY", bootstrap=True,
        apply="restart", label="Update signing key file", group="Updates", advanced=True),
     "dashboard", {}),
    # Features
    (_s("work.v2", bool, True, env="ROOK_WORK_V2", bootstrap=True, apply="restart",
        label="Worklog view (live terminals)", group="Features",
        help="Off keeps only the classic Work view."), "dashboard", {}),
    (_s("work.import_workers", list, [], env="ROOK_WORK_IMPORT_WORKERS", bootstrap=True,
        apply="restart", label="Session import allowlist", group="Features", advanced=True,
        help="Workers whose sessions the Work view imports; empty = all."), "dashboard", {}),
    # Storage
    (_s("store.data_dir", "path", "", env="ROOK_DATA_DIR", bootstrap=True, apply="restart",
        label="Data directory", group="Storage",
        help="One directory for every hub store. Both hub processes must share it."),
     "mcp", {}),
    (_s("store.chat_db", "path", "", env="ROOK_CHAT_DB", bootstrap=True, apply="restart",
        label="Chat database", group="Storage", advanced=True,
        help="Blank: beside the MCP journal. The dashboard follows the MCP's choice."),
     "mcp", {}),
    (_s("store.persist_path", "path", "", env="ROOK_MCP_PERSIST", flag="--persist-path",
        bootstrap=True, apply="restart", label="Token store file", group="Storage",
        advanced=True), "mcp", {}),
    (_s("store.journal_path", "path", "", env="ROOK_MCP_JOURNAL", flag="--journal-path",
        bootstrap=True, apply="restart", label="Journal file", group="Storage",
        advanced=True), "mcp", {}),
    (_s("store.enrollment_db", "path", "", env="ROOK_ENROLLMENT_DB", bootstrap=True,
        apply="restart", label="Band enrollment database", group="Storage", advanced=True),
     "mcp", {}),
    (_s("store.setup_path", "path", "", env="ROOK_SETUP_PATH", bootstrap=True,
        apply="restart", label="Legacy setup file", group="Storage", advanced=True),
     "dashboard", {}),
    # Plugins on the hub
    (_s("hub.plugins", bool, True, env="ROOK_HUB_PLUGINS", bootstrap=True, apply="restart",
        label="Hub plugins (worker 'rook')", group="Plugins"), "mcp", {}),
    (_s("hub.band_max_risk", "str", "read", env="ROOK_HUB_BAND_MAX_RISK", choices=RISKS,
        bootstrap=True, apply="restart", label="Highest risk callable over the band",
        group="Plugins", advanced=True), "mcp", {}),
    (_s("settings.service_readers", dict, {}, label="Services that may fetch settings",
        group="Plugins",
        help='{"voice": ["<token label or agent_id>"]}: tokens allowed to call '
             'settings.fetch for that plugin, secrets included.'), "mcp", {}),
    # Band (managed in the enrollment database; shown here with its source)
    (_s("band.key", "str", secret=True, scope="band", env="ROOK_BAND_PSK", flag="--psk",
        bootstrap=True, apply="risky", label="Band key", group="Keys & access",
        help="Rotate or revoke on the Bands page. The environment only seeds the first "
             "band; it never undoes a rotation."), "mcp", {"managed": "enrollment"}),
    # Worker defaults (band scope, overridable per worker; delivered by config push)
    (_s("worker.announce_interval", int, 30, scope="band", overridable=("worker",),
        flag="--announce-interval", min=5, max=3600, apply="restart",
        label="Announce interval (s)", group="Worker defaults"),
     "worker", {"config_key": "announce_interval"}),
    (_s("worker.log_level", "str", "warning", scope="band", overridable=("worker",),
        choices=("error", "warning", "info", "debug"), apply="restart", flag="-v",
        label="Log level", group="Worker defaults"), "worker", {"config_key": "log_level"}),
    (_s("worker.update_poll", int, 300, scope="band", overridable=("worker",),
        env="ROOK_UPDATE_POLL", min=30, apply="restart", label="Update check interval (s)",
        group="Worker defaults", advanced=True), "worker", {}),
    (_s("worker.authz_mode", "str", "audit", scope="band", overridable=("worker",),
        env="ROOK_AUTHZ_MODE", choices=("off", "audit", "enforce"), apply="restart",
        label="Worker permission checks", group="Worker defaults",
        help="audit: log would-be denials; enforce: refuse them (docs/design/permissions.md)."),
     "worker", {}),
    (_s("worker.authz_allow_unsigned_repoint", bool, False, scope="band", overridable=("worker",),
        env="ROOK_AUTHZ_ALLOW_UNSIGNED_REPOINT", apply="restart", advanced=True,
        label="Allow unsigned hub/PSK changes", group="Worker defaults",
        help="Escape hatch: accept a hub or band-key change without a signed hub order."),
     "worker", {}),
    (_s("worker.name", "str", "", scope="worker", flag="--name", apply="restart",
        label="Worker name", group="Identity",
        help="Blank keeps the name the worker was started with."),
     "worker", {"config_key": "name"}),
]

# -- services beside the hub ------------------------------------------------

_VOICE: list[Setting] = [
    _s("bind", "str", "127.0.0.1", env="VOICE_BIND", bootstrap=True, apply="restart",
       label="Listen address", group="Access"),
    _s("port", int, 8900, env="VOICE_PORT", bootstrap=True, apply="restart", min=1,
       max=65535, label="Port", group="Access"),
    _s("token", "str", secret=True, env="VOICE_TOKEN", apply="restart",
       label="Client bearer token", group="Access"),
    _s("allow_anonymous", bool, False, env="VOICE_ALLOW_ANONYMOUS", apply="restart",
       label="Allow clients without a token", group="Access"),
    _s("whisper_model", "str", "small.en", env="WHISPER_MODEL", apply="reload",
       label="Speech recognition model", group="Recognition"),
    _s("whisper_device", "str", "cpu", env="WHISPER_DEVICE", apply="reload",
       label="Recognition device", group="Recognition"),
    _s("whisper_compute", "str", "int8", env="WHISPER_COMPUTE", apply="reload",
       label="Compute type", group="Recognition"),
    _s("min_speech_ms", int, 450, env="MIN_SPEECH_MS", min=0, label="Minimum speech (ms)",
       group="Recognition", advanced=True),
    _s("min_rms", float, 0.008, env="MIN_RMS", min=0.0, label="Energy floor",
       group="Recognition", advanced=True),
    _s("max_no_speech", float, 0.6, env="MAX_NO_SPEECH", min=0.0, max=1.0,
       label="No-speech ceiling", group="Recognition", advanced=True),
    _s("min_logprob", float, -1.0, env="MIN_LOGPROB", label="Minimum log-probability",
       group="Recognition", advanced=True),
    _s("default_voice", "str", "af_heart", env="VOICE", overridable=("user",),
       label="Voice", group="Speech"),
    _s("show_thinking", bool, False, scope="user", label="Show thinking", group="Speech"),
    _s("hotword_enabled", bool, True, scope="user", label="Wake word on this account",
       group="Speech"),
    _s("llm_url", "url", "http://127.0.0.1:1234/v1/chat/completions", env="VLLM_URL",
       label="Language model URL", group="Agent"),
    _s("llm_model", "str", "", env="VLLM_MODEL", label="Model id", group="Agent"),
    _s("mcp_url", "url", "http://127.0.0.1:8765/mcp", env="ROOK_MCP_URL",
       label="Rook MCP URL", group="Agent"),
    _s("mcp_token", "str", secret=True, env="ROOK_MCP_TOKEN", label="Rook MCP token",
       group="Agent"),
    _s("acp_host", "str", "127.0.0.1", env="ACP_HOST", label="ACP host", group="Agent"),
    _s("acp_port", int, 9200, env="ACP_PORT", min=1, max=65535, label="ACP port",
       group="Agent"),
    _s("acp_auto_approve", bool, True, env="ACP_AUTO_APPROVE",
       label="Auto-approve ACP prompts", group="Agent",
       help="Unattended tool permission for the voice agent."),
    _s("direct_tool_budget", int, 1, env="DIRECT_TOOL_BUDGET", min=0,
       label="Direct tool calls per turn", group="Agent", advanced=True),
]

_DECISION: list[Setting] = [
    _s("url", "str", "", env="DECISION_URL", label="Engine endpoint", group="General",
       help="Blank turns the engine off."),
    _s("assistant_names", list, ["rook", "assistant"], env="DECISION_ASSISTANT_NAMES",
       label="Assistant names", group="General"),
    _s("timeout_ms", int, 150, env="DECISION_TIMEOUT_MS", min=10, max=5000,
       label="Per-turn timeout (ms)", group="Timing"),
    _s("recent_speech_seconds", int, 15, env="DECISION_RECENT_SPEECH_SECONDS", min=1,
       label="Recent speech window (s)", group="Timing"),
    _s("silence_seconds", int, 15, env="DECISION_SILENCE_SECONDS", min=1,
       label="Silence window (s)", group="Timing"),
    _s("raw_retention_days", int, 30, env="DECISION_RAW_RETENTION_DAYS", min=0,
       label="Keep raw inputs (days)", group="Data"),
]

_WATCHDOG: list[Setting] = [
    _s("mcp_url", "str", "http://127.0.0.1:8765", env="ROOK_WATCHDOG_MCP_URL",
       bootstrap=True, label="MCP URL to probe", group="Probe"),
    _s("host", "str", "", env="ROOK_WATCHDOG_HOST", bootstrap=True, label="Host name",
       group="Probe"),
    _s("name", "str", "hub", env="ROOK_WATCHDOG_NAME", bootstrap=True, label="Watchdog name",
       group="Probe"),
    _s("state", "path", "/var/lib/rook-watchdog/state.json", env="ROOK_WATCHDOG_STATE",
       bootstrap=True, label="State file", group="Probe", advanced=True),
    _s("evict_per_min", int, 10, env="ROOK_WATCHDOG_EVICT_PER_MIN", bootstrap=True,
       label="Session evictions per minute before alerting", group="Thresholds"),
    _s("min_mem_mb", int, 80, env="ROOK_WATCHDOG_MIN_MEM_MB", bootstrap=True,
       label="Minimum free memory (MB)", group="Thresholds"),
    _s("repeat_min", int, 60, env="ROOK_WATCHDOG_REPEAT_MIN", bootstrap=True,
       label="Repeat an alert after (min)", group="Thresholds"),
    _s("telegram_token", "str", secret=True, env="ROOK_WATCHDOG_TELEGRAM_TOKEN",
       bootstrap=True, label="Telegram bot token", group="Alerts"),
    _s("telegram_chat", "str", "", env="ROOK_WATCHDOG_TELEGRAM_CHAT", bootstrap=True,
       label="Telegram chat id", group="Alerts"),
    _s("via_hub", bool, False, env="ROOK_WATCHDOG_VIA_HUB", bootstrap=True,
       label="Alert through the hub's notify.send first", group="Alerts",
       help="Uses the Telegram/Discord integration plugins; the direct Telegram settings "
            "above are the fallback when the hub cannot deliver."),
]

#: The relay (telesthete-hub) is a separate binary with its own environment:
#: shown and checked here, not managed (maintainer decision).
_RELAY: list[Setting] = [
    _s("bind", "hostport", "0.0.0.0:7474", env="HUB_BIND", bootstrap=True, apply="restart",
       label="Relay listen address", group="General",
       help="Set in the relay's environment (compose or its unit)."),
    _s("peer_ttl", int, 60, env="HUB_PEER_TTL_SECS", bootstrap=True, apply="restart", min=60,
       label="Peer TTL (s)", group="General",
       help="Keep at 60 or more: peers keep alive every 20 s (the relay's own default, 15, "
            "drops idle peers)."),
    _s("prune", int, 10, env="HUB_PRUNE_SECS", bootstrap=True, apply="restart", min=1,
       label="Prune interval (s)", group="General", advanced=True),
]

_SERVICES: list[tuple[str, str, str, list[Setting]]] = [
    ("relay", "relay", "Relay (display only)", _RELAY),
    # (namespace, owner, page title, settings)
    ("voice", "service:voice", "Voice", _VOICE),
    ("decision", "service:decision", "Decision engine", _DECISION),
    ("watchdog", "watchdog", "Watchdog", _WATCHDOG),
]

PLUGIN_TITLES = {ns: title for ns, _, title, _ in _SERVICES}
PLUGIN_TITLES.update({"knowledge": "Knowledge", "task": "Tasks", "hub": "Hub info",
                      "decide": "Decide (decision model)",
                      "pikvm": "PiKVM", "cec": "HDMI-CEC", "agent": "Wake (agent.wake)"})


def _plugin_classes(package: str = "rook.worker.plugins") -> list[tuple[str, type]]:
    """Every Plugin class with a settings schema defined under ``package``
    (loaded or not: a plugin that is off must still be switchable on)."""
    out = []
    try:
        pkg = importlib.import_module(package)
    except Exception:
        log.exception("cannot import %s for its settings schema", package)
        return out
    for info in pkgutil.iter_modules(pkg.__path__):
        if info.name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"{package}.{info.name}")
        except Exception:
            log.debug("worker plugin %s does not import on the hub", info.name, exc_info=True)
            continue
        seen: set[int] = set()
        for _, obj in inspect.getmembers(mod, inspect.isclass):
            if id(obj) in seen:
                continue
            seen.add(id(obj))
            if (issubclass(obj, Plugin) and obj is not Plugin and obj.__module__ == mod.__name__
                    and obj.NAMESPACE and obj.SETTINGS):
                out.append((info.name, obj))
    return out


class Schema:
    """The merged registry. Build once per process; cheap to query."""

    def __init__(self, hub_plugins: list | None = None, worker_package: str | None =
                 "rook.worker.plugins", hub_package: str | None = "rook.hub.plugins") -> None:
        self.entries: dict[str, Entry] = {}
        for s, owner, extra in _CORE:
            self._add(Entry(f"core.{s.name}", "core", s, owner, "core", **extra))
        for ns, owner, _title, settings in _SERVICES:
            for s in settings:
                self._add(Entry(f"{ns}.{s.name}", ns, s, owner, "service", plugin=ns))
        hub = [(getattr(p, "_module", "") or p.NAMESPACE, type(p)) for p in hub_plugins or []]
        if hub_package:
            hub += _plugin_classes(hub_package)
        for module, cls in hub:
            for s in getattr(cls, "SETTINGS", ()):
                key = f"{cls.NAMESPACE}.{s.name}"
                if key not in self.entries:
                    self._add(Entry(key, cls.NAMESPACE, s, "mcp", "hub-plugin",
                                    plugin=cls.NAMESPACE))
        if worker_package:
            for module, cls in _plugin_classes(worker_package):
                for s in cls.SETTINGS:
                    self._add(Entry(f"{cls.NAMESPACE}.{s.name}", cls.NAMESPACE, s, "worker",
                                    "worker-plugin", plugin=module))

    def _add(self, e: Entry) -> None:
        if e.key in self.entries:
            log.warning("setting %s declared twice; keeping the first", e.key)
            return
        self.entries[e.key] = e

    def get(self, key: str) -> Entry | None:
        return self.entries.get(key)

    def __iter__(self):
        return iter(self.entries.values())

    def plugins(self) -> dict[str, dict]:
        """Plugin pages: ``{namespace: {title, origin, owner, keys}}``."""
        out: dict[str, dict] = {}
        for e in self:
            if e.origin == "core":
                continue
            page = out.setdefault(e.namespace, {
                "namespace": e.namespace,
                "title": PLUGIN_TITLES.get(e.namespace, (e.plugin or e.namespace).capitalize()),
                "origin": e.origin, "owner": e.owner, "module": e.plugin, "keys": []})
            page["keys"].append(e.key)
        return out

    def applies_at(self, e: Entry, scope: str) -> bool:
        return e.setting.scope == scope or scope in e.setting.overridable

    def worker_env_names(self) -> set[str]:
        """Env names of non-secret worker-delivered settings (visible in
        worker.config_get; everything else is masked)."""
        return {n for e in self if e.owner == "worker" and not e.setting.secret
                for n in e.env_names()}
