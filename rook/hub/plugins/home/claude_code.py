"""The home agent's Claude Code backend (``home.backend = claude-code``).

A turn runs on an *agent host*: a worker with the ``agenthost`` plugin and a
logged-in ``claude`` (docs/design/huddles.md 3). The hub keeps everything
else: the prompt, memory, persona and tools. For each turn it

1. picks the host (``home.claude_worker``, then ``home.claude_fallback_workers``,
   the first that is online and offers ``agenthost.turn``);
2. registers the turn's tools on ``/home/turn-mcp`` under a fresh token
   (:mod:`.turn_mcp`), so the model's tool calls come back here and run as the
   home agent exactly as with the OpenAI-compatible backend;
3. calls ``agenthost.turn`` on the host as hub system code (the host's exec cap
   is the hub's to use, not whoever asked), and drops the token afterwards.

The conversation is flattened into one prompt: Claude Code turns are
stateless here (``--no-session-persistence``), because the hub already sends
the room context and memory every time, and a move to another host loses
nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from typing import Any, Awaitable, Callable

from .turn_mcp import PATH, Registry

log = logging.getLogger("rook.hub.plugins.home.claude_code")

#: Model aliases offered on the page (Claude Code resolves them to the
#: current model of that family); any full model id is accepted too.
MODELS = ("haiku", "sonnet", "opus")
ON_UNAVAILABLE = ("fallback_backend", "fail")
AUTH = ("login", "token")
#: A worker counts as online when it was heard from this recently.
ONLINE_S = 120.0
#: Extra time the band call waits beyond the turn's own timeout.
CALL_SLACK_S = 30.0


class Unavailable(RuntimeError):
    """No agent host can run the turn right now."""


class TurnFailed(RuntimeError):
    """The agent host ran the turn and it failed."""


def flatten(messages: list[dict]) -> tuple[str, str]:
    """OpenAI-style messages as ``(system, prompt)`` for one ``claude -p``.

    System messages join into the system prompt. A single user message is the
    prompt as is; a conversation becomes a transcript with the last message
    marked as the one to answer."""
    system = "\n\n".join(str(m.get("content") or "") for m in messages
                         if m.get("role") == "system" and m.get("content"))
    rest = [m for m in messages if m.get("role") in ("user", "assistant") and m.get("content")]
    if len(rest) == 1 and rest[0]["role"] == "user":
        return system, str(rest[0]["content"])
    lines = []
    for m in rest[:-1]:
        who = "You" if m["role"] == "assistant" else "Them"
        lines.append(f"{who}: {m['content']}" if m["role"] == "assistant"
                     else str(m["content"]))
    last = rest[-1]["content"] if rest else ""
    prompt = ("Conversation so far (your own earlier replies are marked 'You:'):\n\n"
              + "\n".join(lines) + "\n\nThe message to answer now:\n" + str(last)
              + "\n\nWrite only your reply.")
    return system, prompt


class ClaudeCodeBackend:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.registry = Registry()
        #: The hub's public base URL (set by the MCP server from --public-url).
        self.public_url = ""

    # -- hosts -------------------------------------------------------------
    def _roster(self) -> dict[str, dict]:
        client = getattr(self.plugin._node, "client", None)
        try:
            return dict(getattr(client, "workers", {}) or {})
        except Exception:  # noqa: BLE001
            return {}

    def hosts(self) -> list[dict]:
        """Workers that can host turns: ``{worker_id, name, online, claude,
        logged_in, running}``, for the page's host picker."""
        now = time.time()
        out = []
        for wid, e in self._roster().items():
            if "agenthost.turn" not in (e.get("caps") or []):
                continue
            hb = (e.get("hb") or {}).get("agenthost") or {}
            out.append({"worker_id": wid, "name": e.get("name") or wid,
                        "online": now - float(e.get("last_seen") or 0) <= ONLINE_S,
                        "claude": hb.get("claude"), "logged_in": hb.get("logged_in"),
                        "running": hb.get("running")})
        return sorted(out, key=lambda h: h["name"])

    def pick(self, cfg: dict) -> tuple[str, str]:
        """``(worker_id, name)`` of the first usable host, or Unavailable."""
        wanted = [str(cfg.get("claude_worker") or "").strip()]
        wanted += [str(w).strip() for w in (cfg.get("claude_fallback_workers") or [])]
        wanted = [w for w in wanted if w]
        if not wanted:
            raise Unavailable("no agent host is set (home.claude_worker)")
        hosts = self.hosts()
        tried = []
        for w in wanted:
            h = next((h for h in hosts if w in (h["worker_id"], h["name"])), None)
            if h is None:
                tried.append(f"{w}: no agenthost plugin (or not in the band)")
            elif not h["online"]:
                tried.append(f"{w}: offline")
            elif h["logged_in"] is False and cfg.get("claude_auth") != "token":
                tried.append(f"{w}: claude is not logged in")
            else:
                return h["worker_id"], h["name"]
        raise Unavailable("no agent host is available (" + "; ".join(tried) + ")")

    def turn_url(self, cfg: dict) -> str:
        base = (str(cfg.get("claude_callback_url") or "").strip() or self.public_url
                or self._setting("core.mcp.public_url"))
        if not base:
            raise Unavailable("the hub's public URL is unknown; set home.claude_callback_url")
        base = base.rstrip("/")
        if base.endswith("/mcp"):
            base = base[:-4]
        return base + PATH

    @staticmethod
    async def _cancel(client: Any, wid: str, turn_id: str, who: dict) -> None:
        with contextlib.suppress(Exception):
            await client.call(cap="agenthost.cancel", args={"turn_id": turn_id}, target=wid,
                              timeout=15.0, **who)

    def _setting(self, key: str) -> str:
        svc = getattr(self.plugin._node, "settings", None)
        with contextlib.suppress(Exception):
            return str((svc.resolve(key) or {}).get("value") or "")
        return ""

    # -- a turn ------------------------------------------------------------
    async def run(self, cfg: dict, messages: list[dict], *, tools: list[dict],
                  handler: Callable[[str, dict], Awaitable[str]], via: str,
                  max_tool_calls: int = 10) -> dict:
        """One turn. Returns ``{content, model, latency_ms, tools_used, worker,
        usage, cost_usd}``; raises Unavailable or TurnFailed."""
        wid, name = self.pick(cfg)
        url = self.turn_url(cfg)
        system, prompt = flatten(messages)
        timeout = float(cfg.get("claude_timeout_s") or 180.0)
        oauth = None
        if cfg.get("claude_auth") == "token":
            oauth = self.plugin._api_key(cfg.get("claude_token"), via="home agent (claude token)")
            if not oauth:
                raise Unavailable("home.claude_auth is token but home.claude_token is not set")
        token, turn = self.registry.open(tools, handler, timeout)
        turn_id = f"{via}-{uuid.uuid4().hex[:16]}"
        args = {"turn_id": turn_id, "prompt": prompt, "system": system or "You are a helpful agent.",
                "mcp_url": url, "mcp_token": token, "model": str(cfg.get("claude_model") or "haiku"),
                "timeout_s": timeout, "max_tool_calls": int(max_tool_calls)}
        effort = str(cfg.get("claude_effort") or "").strip()
        if effort:
            args["effort"] = effort
        if oauth:
            args["oauth_token"] = oauth
        from ...authz import system as system_principal
        t0 = time.monotonic()
        who = {"identity": f"system:home.{self.plugin.agent_name}",
               "principal": system_principal(f"home.{self.plugin.agent_name}")}
        client = self.plugin._node.client
        try:
            reply = await client.call(cap="agenthost.turn", args=args, target=wid,
                                      timeout=timeout + CALL_SLACK_S, **who)
        except asyncio.CancelledError:
            # The caller gave up (a job step's own timeout): stop the process too.
            asyncio.get_running_loop().create_task(self._cancel(client, wid, turn_id, who))
            raise
        finally:
            self.registry.close(token)
        if not reply.get("ok", True) and "result" not in reply:
            raise TurnFailed(f"{name}: {reply.get('error') or 'the agent host refused the turn'}")
        res = reply.get("result", reply)
        if not isinstance(res, dict):
            raise TurnFailed(f"{name}: unexpected reply")
        if not res.get("ok"):
            raise TurnFailed(f"{name}: {res.get('error') or 'the turn failed'}")
        return {"content": str(res.get("text") or "").strip(),
                "model": str(res.get("model") or args["model"]),
                "latency_ms": round((time.monotonic() - t0) * 1000),
                "tools_used": list(turn.used), "worker": name,
                "usage": res.get("usage") or {}, "cost_usd": res.get("cost_usd")}
