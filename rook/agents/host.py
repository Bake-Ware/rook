"""The session host: roles, their lanes, side threads, the voice server and the link."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import VERSION, roledir
from .config import Config
from .lane import LANES, Busy, Lane, LaneError, LaneSpec
from .link import Link, OpError
from .side import Side
from .voice import VoiceRoom, VoiceServer

log = logging.getLogger("rook.agents.host")

SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
MAX_TEXT = 400_000
#: Changes under these never affect the main lane / always affect the voice lane.
VOICE_DIR = "voice/"
VOICE_INHERITS = ("CLAUDE.md", "CLAUDE.local.md")


def version() -> str:
    rev = ""
    with contextlib.suppress(OSError):
        rev = (Path(__file__).resolve().parents[2] / "REVISION").read_text().strip()[:40]
    return f"{VERSION}+{rev}" if rev else VERSION


@dataclass
class RoleState:
    role: str
    dir: Path
    lanes: dict[str, Lane] = field(default_factory=dict)
    voice_hash: str | None = None
    voice: VoiceRoom | None = None
    host_servers: list[str] = field(default_factory=list)
    memory: str = "off"
    missing_servers: list[str] = field(default_factory=list)


class Host:
    def __init__(self, cfg: Config, token: str = "") -> None:
        self.cfg = cfg
        self.roles: dict[str, RoleState] = {}
        self.side = Side(self)
        self.voice_server = VoiceServer(self)
        self.link: Link | None = None
        if cfg.hub_url:
            self.link = Link(cfg.hub_url, token, self.hello, self.handle,
                             backoff=cfg.link_backoff, buffer=cfg.buffer_events)
        #: Extra event listeners (tests, diagnostics).
        self.listeners: list[Callable[[dict], None]] = []
        self._stop = asyncio.Event()
        self._started = time.time()
        self._configure_lock = asyncio.Lock()
        Path(cfg.roles_dir).mkdir(parents=True, exist_ok=True)

    # -- events -------------------------------------------------------------------
    def emit(self, role: str, lane: str, kind: str, **fields) -> None:
        msg = {"t": "ev", "role": role, "lane": lane, "kind": kind, **fields,
               "ts": round(time.time(), 3)}
        if self.link is not None:
            self.link.send(msg)
        for fn in list(self.listeners):
            with contextlib.suppress(Exception):
                fn(msg)

    def servers(self) -> list[str]:
        return sorted(roledir.user_servers(self.cfg.claude_user_config))

    def hello(self) -> dict:
        out = {"t": "hello", "host": self.cfg.host, "version": version(), "pid": os.getpid(),
               "servers": self.servers(),
               "lanes": {ln.key: ln.state for rs in self.roles.values()
                         for ln in rs.lanes.values()}}
        missing = {r: rs.missing_servers for r, rs in self.roles.items() if rs.missing_servers}
        if missing:
            out["missing_servers"] = missing
        return out

    # -- ops ----------------------------------------------------------------------
    async def handle(self, msg: dict) -> Any:
        t = msg.get("t")
        fn = getattr(self, f"op_{t}", None) if isinstance(t, str) and t.isidentifier() else None
        if fn is None:
            raise OpError(f"unknown request {t!r}")
        try:
            return await fn(msg)
        except Busy:
            raise OpError("busy") from None
        except (LaneError, roledir.RoleDirError) as e:
            raise OpError(str(e)) from None

    def _lane(self, msg: dict) -> Lane:
        role = roledir.check_role(msg.get("role"))
        name = str(msg.get("lane") or "main")
        rs = self.roles.get(role)
        lane = rs.lanes.get(name) if rs else None
        if lane is None:
            raise OpError(f"no lane {role}/{name} here")
        return lane

    @staticmethod
    def _text(msg: dict, key: str = "text") -> str:
        text = msg.get(key)
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            raise OpError(f"{key}: text expected")
        return text

    async def op_configure(self, msg: dict) -> dict:
        roles = msg.get("roles")
        if not isinstance(roles, dict):
            raise OpError("roles: {role: {dir_files, lanes, voice_token_sha256}}")
        plans = []
        for role, rc in roles.items():
            roledir.check_role(role)
            if not isinstance(rc, dict):
                raise OpError(f"{role}: an object")
            files = roledir.validate_files(rc.get("dir_files"))
            lanes_in = rc.get("lanes") or {}
            if not isinstance(lanes_in, dict) or any(n not in LANES for n in lanes_in):
                raise OpError(f"{role}: lanes are main and voice")
            specs = {n: LaneSpec.parse(s) for n, s in lanes_in.items()}
            vh = rc.get("voice_token_sha256")
            if vh is not None and vh != "" and not SHA256.match(str(vh)):
                raise OpError(f"{role}: voice_token_sha256 is a sha256 hex digest")
            hs = rc.get("host_servers") or []
            if not isinstance(hs, list) or not all(isinstance(n, str) and
                                                   roledir.SERVER_NAME.match(n) for n in hs):
                raise OpError(f"{role}: host_servers is a list of server names")
            memory = str(rc.get("memory") or "off")
            if memory not in roledir.MEMORY_MODES:
                raise OpError(f"{role}: memory is write, read or off")
            plans.append((role, files, specs, (str(vh).lower() if vh else None), list(hs),
                          memory))
        out = {}
        async with self._configure_lock:
            for role, files, specs, vh, hs, memory in plans:
                out[role] = await self._configure_role(role, files, specs, vh, hs, memory)
        return {"roles": out}

    async def _configure_role(self, role: str, files: dict[str, str], specs: dict[str, LaneSpec],
                              voice_hash: str | None, host_servers: list[str],
                              memory: str) -> dict:
        rdir = roledir.ensure(self.cfg.roles_dir, role)
        rs = self.roles.get(role)
        if rs is None:
            rs = self.roles[role] = RoleState(role, rdir)
        rs.voice_hash = voice_hash
        rs.host_servers, rs.memory = host_servers, memory
        # The hub's .mcp.json is kept aside; the written one has host servers merged in.
        hub_mcp = files.pop(roledir.MCP_FILE, None)
        hub_path = rdir / roledir.HUB_MCP
        if hub_mcp is not None:
            if roledir.read_text(hub_path) != hub_mcp:
                roledir.write_text(hub_path, hub_mcp)
        else:
            hub_mcp = roledir.read_text(hub_path)
        changed = roledir.write_files(rdir, files)
        merged, rs.missing_servers = roledir.merged_mcp(
            rdir, hub_mcp, host_servers, roledir.user_servers(self.cfg.claude_user_config),
            memory)
        if merged is not None:
            changed += roledir.write_files(rdir, {roledir.MCP_FILE: merged})
        lanes_out: dict[str, str] = {}
        for name, spec in specs.items():
            cwd = rdir if name == "main" else rdir / "voice"
            lane = rs.lanes.get(name)
            touched = any(
                (c.startswith(VOICE_DIR) or c in VOICE_INHERITS) if name == "voice"
                else not c.startswith(VOICE_DIR) for c in changed)
            if lane is None:
                lane = rs.lanes[name] = Lane(self, role, name, cwd, spec)
                await lane.start()
                lanes_out[name] = "started"
                continue
            spec_changed = spec.key() != lane.spec.key()
            if not spec.session_id:
                spec.session_id = lane.spec.session_id
            lane.spec = spec
            if lane.state == "stopped":
                await lane.start()
                lanes_out[name] = "started"
            elif spec_changed or touched:
                lanes_out[name] = "reload " + await lane.reload()
            else:
                lanes_out[name] = "unchanged"
        for name in [n for n in rs.lanes if n not in specs]:
            await rs.lanes.pop(name).stop()
            lanes_out[name] = "removed"
        if "voice" in rs.lanes and rs.voice is None:
            rs.voice = VoiceRoom(self, role)
        elif "voice" not in rs.lanes and rs.voice is not None:
            await rs.voice.close()
            rs.voice = None
        res: dict[str, Any] = {"changed": changed, "lanes": lanes_out}
        if rs.missing_servers:
            res["missing_servers"] = rs.missing_servers
        return res

    async def op_send(self, msg: dict) -> dict:
        lane = self._lane(msg)
        item = str(msg.get("item") or "")
        if not item:
            raise OpError("item: the item id")
        await lane.send(item, self._text(msg))
        return {"item": item, "session_id": lane.session_id}

    async def op_steer(self, msg: dict) -> dict:
        return await self._lane(msg).steer(self._text(msg))

    async def op_interrupt(self, msg: dict) -> dict:
        return {"interrupted": await self._lane(msg).interrupt()}

    async def op_compact(self, msg: dict) -> dict:
        lane = self._lane(msg)
        instr = msg.get("instructions")
        await lane.compact(instr if isinstance(instr, str) else "")
        return {"compacting": True}

    async def op_reload(self, msg: dict) -> dict:
        return {"reload": await self._lane(msg).reload()}

    async def op_stop(self, msg: dict) -> dict:
        role = roledir.check_role(msg.get("role"))
        rs = self.roles.get(role)
        if rs is None:
            raise OpError(f"no role {role} here")
        for lane in rs.lanes.values():
            await lane.stop()
        return {"stopped": sorted(rs.lanes)}

    async def op_fork(self, msg: dict) -> dict:
        return await self.side.fork(msg)

    async def op_fork_cancel(self, msg: dict) -> dict:
        return await self.side.cancel(str(msg.get("fork") or ""))

    async def op_btw(self, msg: dict) -> dict:
        return self.side.btw(msg)

    async def op_voice_say(self, msg: dict) -> dict:
        role = roledir.check_role(msg.get("role"))
        rs = self.roles.get(role)
        if rs is None or rs.voice is None:
            raise OpError(f"role {role} has no voice lane here")
        turn = msg.get("turn")
        if not (isinstance(turn, int) and not isinstance(turn, bool)):
            turn = str(turn) if turn else None      # numbers stay numbers (the voice client's ids)
        return rs.voice.say(self._text(msg), turn)

    async def op_status(self, msg: dict) -> dict:
        return self.status()

    def status(self) -> dict:
        out: dict[str, Any] = {
            "host": self.cfg.host, "version": version(), "pid": os.getpid(),
            "uptime_s": round(time.time() - self._started),
            "lanes": {ln.key: ln.status() for rs in self.roles.values()
                      for ln in rs.lanes.values()},
            "forks": self.side.status(),
            "voice_clients": {r: len(rs.voice.clients) for r, rs in self.roles.items()
                              if rs.voice is not None},
            "servers": self.servers()}
        missing = {r: rs.missing_servers for r, rs in self.roles.items() if rs.missing_servers}
        if missing:
            out["missing_servers"] = missing
        return out

    # -- running ------------------------------------------------------------------
    async def start(self) -> None:
        addr = self.cfg.voice_addr()
        if addr is not None:
            await self.voice_server.start(addr)

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        await self.start()
        link_task = asyncio.create_task(self.link.run()) if self.link is not None else None
        try:
            await self._stop.wait()
        finally:
            # Lanes first, so their last events (stopped, failed items) reach the hub.
            await self.close()
            if link_task is not None:
                link = self.link
                assert link is not None
                end = time.monotonic() + 3
                while link.buf and link.connected.is_set() and time.monotonic() < end:
                    await asyncio.sleep(0.05)
                await link.close()
                link_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await link_task

    async def close(self) -> None:
        await self.side.close()
        for rs in self.roles.values():
            if rs.voice is not None:
                await rs.voice.close()
        await asyncio.gather(*(ln.stop() for rs in self.roles.values()
                               for ln in rs.lanes.values()), return_exceptions=True)
        await self.voice_server.stop()
