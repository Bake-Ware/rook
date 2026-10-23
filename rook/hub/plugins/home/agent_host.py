"""The home agent on an agent harness: ``home.backend`` = ``claude-code``,
``codex`` or ``opencode``.

A turn runs on an *agent host*: a worker whose ``agenthost`` plugin found
that harness installed (docs/design/huddles.md 3). The hub keeps everything
else: the prompt, memory, persona and tools. For each turn it

1. picks the host (``home.agent_host``, then ``home.agent_fallback_hosts``:
   the first that is online, has the harness, and does not report it logged
   out);
2. registers the turn's tools on ``/home/turn-mcp`` under a fresh token
   (:mod:`.turn_mcp`), so the model's tool calls come back here and run as the
   home agent exactly as with the OpenAI-compatible backend;
3. calls ``agenthost.turn`` on the host as hub system code (the host's exec cap
   is the hub's to use, not whoever asked), and drops the token afterwards.

The conversation is flattened into one prompt: turns are stateless here (no
session files), because the hub already sends the room context and memory
every time, and a move to another host loses nothing.

A **role turn** (``role=``, from the ``roles`` plugin; docs/design/huddles.md
4.4) runs on the host that keeps the role's home (no fallback: the home and
its memory are there) and sends the role's tool names, built-ins and memory
mode, from which the agent host renders the harness config in the home.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import os
import logging
import time
import uuid
from typing import Any, Awaitable, Callable

from .turn_mcp import PATH, Registry

log = logging.getLogger("rook.hub.plugins.home.agent_host")

#: home.backend value -> the worker's harness name.
HARNESS = {"claude-code": "claude", "codex": "codex", "opencode": "opencode"}
LABELS = {"claude-code": "Claude Code", "codex": "Codex", "opencode": "OpenCode"}
#: Model suggestions before a host is asked (claude: aliases for the current
#: model of each family). Any model id the harness accepts works.
CLAUDE_MODELS = ("haiku", "sonnet", "opus")
ON_UNAVAILABLE = ("fallback_backend", "fail")
AUTH = ("login", "token")
EFFORTS = ("", "low", "medium", "high", "xhigh", "max")
#: A worker counts as online when it was heard from this recently.
ONLINE_S = 120.0
#: Extra time the band call waits beyond the turn's own timeout.
CALL_SLACK_S = 30.0
#: Turns kept for the page's usage summary (in memory, since the MCP server
#: started; a busy day of huddles is a few hundred).
LEDGER_TURNS = 5000
#: Token counters summed per role, as the harnesses report them.
TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens",
              "cache_read_input_tokens", "cached_input_tokens", "reasoning_tokens")


class Unavailable(RuntimeError):
    """No agent host can run the turn right now."""


class TurnFailed(RuntimeError):
    """The agent host ran the turn and it failed."""


def flatten(messages: list[dict]) -> tuple[str, str]:
    """OpenAI-style messages as ``(system, prompt)`` for one headless run.

    System messages join into the system prompt. A single user message is the
    prompt as is; a conversation becomes a transcript with the last message
    marked as the one to answer."""
    system = "\n\n".join(str(m.get("content") or "") for m in messages
                         if m.get("role") == "system" and m.get("content"))
    rest = [m for m in messages if m.get("role") in ("user", "assistant") and m.get("content")]
    if len(rest) == 1 and rest[0]["role"] == "user":
        return system, str(rest[0]["content"])
    lines = [f"You: {m['content']}" if m["role"] == "assistant" else str(m["content"])
             for m in rest[:-1]]
    last = rest[-1]["content"] if rest else ""
    prompt = ("Conversation so far (your own earlier replies are marked 'You:'):\n\n"
              + "\n".join(lines) + "\n\nThe message to answer now:\n" + str(last)
              + "\n\nWrite only your reply.")
    return system, prompt


def harness_of(cfg: dict) -> str | None:
    return HARNESS.get(str(cfg.get("backend") or ""))


def model_of(cfg: dict) -> str:
    """The model setting for the configured harness ("" = its default)."""
    h = harness_of(cfg)
    key = {"claude": "claude_model", "codex": "codex_model", "opencode": "opencode_model"}.get(h)
    return str(cfg.get(key) or "").strip() if key else ""


def effort_of(cfg: dict) -> str:
    h = harness_of(cfg)
    key = {"claude": "claude_effort", "codex": "codex_effort"}.get(h)
    return str(cfg.get(key) or "").strip() if key else ""


class AgentHostBackend:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.registry = Registry()
        #: The hub's public base URL (set by the MCP server from --public-url).
        self.public_url = ""
        #: One row per turn that reached a host: {ts, role, via, worker, model,
        #: ok, usage, cost_usd} (:meth:`usage`).
        self.ledger: collections.deque = collections.deque(maxlen=LEDGER_TURNS)

    def _book(self, role: dict | None, via: str, worker: str, model: str, ok: bool,
              usage: dict | None = None, cost: Any = None) -> None:
        self.ledger.append({"ts": time.time(), "role": (role or {}).get("id") or "",
                            "via": via, "worker": worker, "model": model, "ok": ok,
                            "usage": dict(usage or {}), "cost_usd": cost})

    def usage(self, hours: float = 24.0) -> list[dict]:
        """Turns and token usage per role over the last ``hours`` (role
        ``""``: the house agent's own turns, chairing included), most turns
        first: ``{role, turns, failed, tokens: {key: n}, cost_usd, models}``."""
        since = time.time() - hours * 3600
        out: dict[str, dict] = {}
        for row in list(self.ledger):
            if row["ts"] < since:
                continue
            r = out.setdefault(row["role"], {"role": row["role"], "turns": 0, "failed": 0,
                                             "tokens": {}, "cost_usd": 0.0, "models": []})
            r["turns"] += 1
            r["failed"] += 0 if row["ok"] else 1
            for k in TOKEN_KEYS:
                v = row["usage"].get(k)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    r["tokens"][k] = r["tokens"].get(k, 0) + int(v)
            if isinstance(row["cost_usd"], (int, float)) and not isinstance(row["cost_usd"], bool):
                r["cost_usd"] = round(r["cost_usd"] + float(row["cost_usd"]), 6)
            if row["model"] and row["model"] not in r["models"]:
                r["models"].append(row["model"])
        return sorted(out.values(), key=lambda r: (-r["turns"], r["role"]))

    # -- hosts -------------------------------------------------------------
    def _roster(self) -> dict[str, dict]:
        client = getattr(self.plugin._node, "client", None)
        try:
            return dict(getattr(client, "workers", {}) or {})
        except Exception:  # noqa: BLE001
            return {}

    def hosts(self) -> list[dict]:
        """Workers that can host turns, for the page's picker: ``{worker_id,
        name, online, harnesses: {name: {version, logged_in}}, running}``."""
        now = time.time()
        out = []
        for wid, e in self._roster().items():
            if "agenthost.turn" not in (e.get("caps") or []):
                continue
            hb = (e.get("hb") or {}).get("agenthost") or {}
            harnesses = hb.get("harnesses") if isinstance(hb.get("harnesses"), dict) else {}
            if not harnesses and hb.get("claude"):          # a build 772 worker
                harnesses = {"claude": {"version": hb.get("claude"),
                                        "logged_in": hb.get("logged_in")}}
            out.append({"worker_id": wid, "name": e.get("name") or wid,
                        "online": now - float(e.get("last_seen") or 0) <= ONLINE_S,
                        "harnesses": harnesses, "running": hb.get("running"),
                        "workspace": hb.get("workspace") or ""})
        return sorted(out, key=lambda h: h["name"])

    def pick(self, cfg: dict, harness: str, only: str | None = None) -> tuple[str, str]:
        """``(worker_id, name)`` of the first usable host, or Unavailable.
        ``only``: that host or nothing (a role whose home is there)."""
        if only:
            wanted = [only]
        else:
            wanted = [str(cfg.get("agent_host") or "").strip()]
            wanted += [str(w).strip() for w in (cfg.get("agent_fallback_hosts") or [])]
        wanted = [w for w in wanted if w]
        if not wanted:
            raise Unavailable("no agent host is set (home.agent_host)")
        hosts = self.hosts()
        tried = []
        for w in wanted:
            h = next((h for h in hosts if w in (h["worker_id"], h["name"])), None)
            if h is None:
                tried.append(f"{w}: no agent harness (or not in the band)")
                continue
            info = h["harnesses"].get(harness)
            if not h["online"]:
                tried.append(f"{w}: offline")
            elif info is None and h["harnesses"]:
                tried.append(f"{w}: {harness} is not installed")
            elif (info or {}).get("logged_in") is False and not (
                    harness == "claude" and cfg.get("claude_auth") == "token"):
                tried.append(f"{w}: {harness} is not logged in")
            else:
                return h["worker_id"], h["name"]
        raise Unavailable("no agent host is available (" + "; ".join(tried) + ")")

    def turn_url(self, cfg: dict) -> str:
        base = (str(cfg.get("agent_callback_url") or "").strip() or self.public_url
                or self._setting("core.mcp.public_url") or self._dashboard_base())
        if not base:
            raise Unavailable("the hub's public URL is unknown; set home.agent_callback_url")
        base = base.rstrip("/")
        if base.endswith("/mcp"):
            base = base[:-4]
        return base + PATH

    def _dashboard_base(self) -> str:
        """The dashboard's public base (``--domain``, what workers already use
        to reach the hub): on a single-process hub it serves the turn server
        too, so a fresh install needs no callback URL of its own."""
        from ...approvals import dashboard_url
        node = self.plugin._node
        with contextlib.suppress(Exception):
            return dashboard_url(getattr(node, "settings_store", None), os.environ)
        return ""

    def _setting(self, key: str) -> str:
        svc = getattr(self.plugin._node, "settings", None)
        with contextlib.suppress(Exception):
            return str((svc.resolve(key) or {}).get("value") or "")
        return ""

    def _who(self) -> dict:
        from ...authz import system as system_principal
        name = self.plugin.agent_name
        return {"identity": f"system:home.{name}",
                "principal": system_principal(f"home.{name}")}

    async def models(self, cfg: dict) -> dict:
        """Model ids for the configured harness, asked of the chosen host."""
        harness = harness_of(cfg) or "claude"
        try:
            wid, name = self.pick(cfg, harness)
        except Unavailable as e:
            return {"ok": True, "models": list(CLAUDE_MODELS) if harness == "claude" else [],
                    "note": str(e)}
        reply = await self.plugin._node.client.call(
            cap="agenthost.models", args={"harness": harness}, target=wid, timeout=45.0,
            **self._who())
        res = reply.get("result") if isinstance(reply.get("result"), dict) else {}
        if not res.get("ok"):
            return {"ok": False, "error": f"{name}: {reply.get('error') or res.get('error')}"}
        return {"ok": True, "models": list(res.get("models") or []), "host": name}

    @staticmethod
    async def _cancel(client: Any, wid: str, turn_id: str, who: dict) -> None:
        with contextlib.suppress(Exception):
            await client.call(cap="agenthost.cancel", args={"turn_id": turn_id}, target=wid,
                              timeout=15.0, **who)

    # -- a turn ------------------------------------------------------------
    async def run(self, cfg: dict, messages: list[dict], *, tools: list[dict],
                  handler: Callable[[str, dict], Awaitable[str]], via: str,
                  max_tool_calls: int = 10, role: dict | None = None) -> dict:
        """One turn. Returns ``{content, model, latency_ms, tools_used, worker,
        worker_id, harness, usage, cost_usd}``; raises Unavailable or TurnFailed.
        ``role`` (a role document) makes it a role turn in the role's home."""
        harness = harness_of(cfg)
        if harness is None:
            raise Unavailable(f"home.backend {cfg.get('backend')!r} is not an agent harness")
        sessions = getattr(self.plugin, "sessions", None)
        if sessions is not None and sessions.accepts(cfg, tools, via, role):
            # The house agent's always-on session, through its inbox
            # (sessions.py); without a session host, the one-shot turn below.
            try:
                res = await sessions.run(cfg, messages, tools=tools, handler=handler, via=via)
            except Unavailable as e:
                log.warning("persistent session unavailable (%s); one-shot turn instead", e)
            else:
                self._book(None, via, res.get("worker") or "", res.get("model") or "", True,
                           res.get("usage"), res.get("cost_usd"))
                return res
        wid, name = self.pick(cfg, harness, only=(role or {}).get("host") or None)
        url = self.turn_url(cfg)
        system, prompt = flatten(messages)
        timeout = float(cfg.get("agent_timeout_s") or 180.0)
        oauth = None
        if harness == "claude" and cfg.get("claude_auth") == "token":
            oauth = self.plugin._api_key(cfg.get("claude_token"), via="home agent (claude token)")
            if not oauth:
                raise Unavailable("home.claude_auth is token but home.claude_token is not set")
        token, turn = self.registry.open(tools, handler, timeout)
        turn_id = f"{via}-{uuid.uuid4().hex[:16]}"
        model = model_of(cfg)
        args = {"turn_id": turn_id, "harness": harness, "prompt": prompt,
                "system": system or "You are a helpful agent.", "mcp_url": url,
                "mcp_token": token, "model": model, "timeout_s": timeout,
                "max_tool_calls": int(max_tool_calls)}
        if effort_of(cfg):
            args["effort"] = effort_of(cfg)
        if oauth:
            args["oauth_token"] = oauth
        if role is not None:
            args.update(role=role["id"], role_name=role.get("name") or role["id"],
                        role_rev=int(role.get("rev") or 0),
                        tools=[(t.get("function") or t)["name"] for t in tools],
                        builtin=list(role.get("builtin") or []),
                        memory=role.get("memory") or "write")
            if role.get("workspace_native"):
                # The host announced hb.agenthost.workspace = "native": its
                # Claude Code gets file tools with workspace/ the only writable root.
                args["workspace"] = True
        who = self._who()
        client = self.plugin._node.client
        t0 = time.monotonic()
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
            self._book(role, via, name, model, False)
            raise TurnFailed(f"{name}: {reply.get('error') or 'the agent host refused the turn'}")
        res = reply.get("result", reply)
        if not isinstance(res, dict):
            self._book(role, via, name, model, False)
            raise TurnFailed(f"{name}: unexpected reply")
        usage = res.get("usage") if isinstance(res.get("usage"), dict) else {}
        if not res.get("ok"):
            self._book(role, via, name, model, False, usage, res.get("cost_usd"))
            raise TurnFailed(f"{name}: {res.get('error') or 'the turn failed'}")
        self._book(role, via, name, str(res.get("model") or model), True, usage,
                   res.get("cost_usd"))
        return {"content": str(res.get("text") or "").strip(),
                "model": str(res.get("model") or model or f"{LABELS[cfg['backend']]} default"),
                "latency_ms": round((time.monotonic() - t0) * 1000),
                "tools_used": list(turn.used), "worker": name, "worker_id": wid,
                "harness": harness, "usage": res.get("usage") or {},
                "cost_usd": res.get("cost_usd"), "harness_tools": list(res.get("tools_used") or [])}
