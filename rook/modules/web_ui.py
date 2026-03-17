"""Web UI module — dashboard + chat interface served from the remote server."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

MODULE_NAME = "web_ui"
MODULE_DESCRIPTION = "Web dashboard and chat interface"
MODULE_TYPE = "channel"

log = logging.getLogger(__name__)

_agent = None
_ui_clients: dict[str, web.WebSocketResponse] = {}  # ws_id -> ws
WEB_DIR = Path(__file__).resolve().parents[1] / "web"


def register_routes(app) -> None:
    """Register routes before server starts. Called from bootstrap server init."""
    app.router.add_get("/api/status", _api_status)
    app.router.add_get("/api/workers", _api_workers)
    app.router.add_get("/api/channels", _api_channels)
    app.router.add_get("/api/goals", _api_goals)
    app.router.add_get("/api/jobs", _api_jobs)
    app.router.add_get("/api/facts", _api_facts)
    app.router.add_get("/api/models", _api_models)
    app.router.add_get("/api/pipeline", _api_pipeline)
    app.router.add_post("/api/pipeline", _api_pipeline_update)
    app.router.add_post("/api/model", _api_model_switch)

    # CRUD endpoints
    app.router.add_post("/api/facts/{fact_id}/promote", _api_fact_promote)
    app.router.add_post("/api/facts/{fact_id}/demote", _api_fact_demote)
    app.router.add_patch("/api/facts/{fact_id}", _api_fact_edit)
    app.router.add_delete("/api/facts/{fact_id}", _api_fact_delete)
    app.router.add_delete("/api/channels/{platform}/{platform_id}", _api_channel_delete)
    app.router.add_delete("/api/goals/{goal_id}", _api_goal_delete)
    app.router.add_post("/api/goals/{goal_id}/pause", _api_goal_pause)
    app.router.add_delete("/api/jobs/{job_id}", _api_job_delete)
    app.router.add_get("/ws/ui", _ws_ui_handler)
    app.router.add_get("/ui/{path:.*}", _serve_static)
    # Note: / route is handled by the bootstrap server, we override it there


async def start(agent: Any, config: Any) -> None:
    global _agent
    _agent = agent

    # Register as a channel sender
    async def web_send(ws_id: str, message: str) -> None:
        ws = _ui_clients.get(ws_id)
        if ws and not ws.closed:
            await ws.send_json({"type": "response", "content": message})

    agent.tools.channel_bridge.register_sender("web", web_send)
    log.info("Web UI routes registered at /ui")


async def stop() -> None:
    for ws in _ui_clients.values():
        if not ws.closed:
            await ws.close()


# ── API Endpoints ────────────────────────────────────────────────────────────

async def _api_status(request: web.Request) -> web.Response:
    from ..memory.sysinfo import get_system_stats
    status = _agent.fact_store.status()
    active = _agent.router.get_active()
    # Active tasks count
    active_agents = sum(1 for a in _agent.agent_pool._agents.values() if a.status == "running")
    active_goals = len([g for g in _agent.tools.goal_store._goals.values() if g.status == "active"])

    return web.json_response({
        "system": get_system_stats(),
        "model": {"name": active.name, "model": active.model, "provider": active.provider},
        "memory": status,
        "pipeline": _agent.pipeline.to_dict(),
        "quota": _agent.router._anthropic_quota,
        "workers": len(_agent.tools.remote_server._workers),
        "facts_total": len(_agent.fact_store.volatile) + len(_agent.fact_store.working) + len(_agent.fact_store.concrete),
        "busy": active_agents > 0,
        "active_agents": active_agents,
        "active_goals": active_goals,
    })


async def _api_workers(request: web.Request) -> web.Response:
    return web.json_response(_agent.tools.remote_server.list_workers())


async def _api_channels(request: web.Request) -> web.Response:
    return web.json_response(_agent.tools.memory_store.list_channels(), dumps=lambda o: json.dumps(o, default=str))


async def _api_goals(request: web.Request) -> web.Response:
    return web.json_response(_agent.tools.goal_store.list_goals())


async def _api_jobs(request: web.Request) -> web.Response:
    return web.json_response(_agent.tools.scheduler.list_jobs(), dumps=lambda o: json.dumps(o, default=str))


async def _api_facts(request: web.Request) -> web.Response:
    tier = request.query.get("tier", "all")
    facts = []
    if tier in ("all", "concrete"):
        for f in _agent.fact_store.concrete:
            facts.append({"id": f.id, "tier": "concrete", "fact": f.fact, "category": f.category, "access_count": f.access_count})
    if tier in ("all", "working"):
        for f in _agent.fact_store.working:
            facts.append({"id": f.id, "tier": "working", "fact": f.fact, "category": f.category, "access_count": f.access_count})
    if tier in ("all", "volatile"):
        for f in _agent.fact_store.volatile:
            facts.append({"id": f.id, "tier": "volatile", "fact": f.fact, "category": f.category, "access_count": f.access_count})
    return web.json_response(facts)


async def _api_models(request: web.Request) -> web.Response:
    return web.json_response(_agent.router.list_models())


async def _api_pipeline(request: web.Request) -> web.Response:
    return web.json_response(_agent.pipeline.to_dict())


async def _api_pipeline_update(request: web.Request) -> web.Response:
    data = await request.json()
    stage = data.get("stage", "")
    kwargs = {k: v for k, v in data.items() if k != "stage"}
    result = _agent.update_pipeline(stage, **kwargs)
    return web.json_response({"result": result, "pipeline": _agent.pipeline.to_dict()})


async def _api_fact_promote(request: web.Request) -> web.Response:
    fact_id = request.match_info["fact_id"]
    result = _agent.fact_store.promote(fact_id=fact_id)
    _agent.fact_store.flush_to_db()
    return web.json_response({"result": result})


async def _api_fact_demote(request: web.Request) -> web.Response:
    fact_id = request.match_info["fact_id"]
    result = _agent.fact_store.demote(fact_id=fact_id)
    _agent.fact_store.flush_to_db()
    return web.json_response({"result": result})


async def _api_fact_edit(request: web.Request) -> web.Response:
    fact_id = request.match_info["fact_id"]
    data = await request.json()
    new_text = data.get("fact", "")
    if not new_text:
        return web.json_response({"error": "fact text required"}, status=400)
    # Find and update in all tiers
    for tier in [_agent.fact_store.volatile, _agent.fact_store.working, _agent.fact_store.concrete]:
        for f in tier:
            if f.id == fact_id:
                f.fact = new_text
                _agent.fact_store.flush_to_db()
                return web.json_response({"result": "updated"})
    return web.json_response({"error": "fact not found"}, status=404)


async def _api_channel_delete(request: web.Request) -> web.Response:
    platform = request.match_info["platform"]
    platform_id = request.match_info["platform_id"]
    try:
        _agent.tools.memory_store._db.execute(
            "DELETE FROM channels WHERE platform = ? AND platform_id = ?",
            (platform, platform_id),
        )
        _agent.tools.memory_store._db.commit()
        return web.json_response({"result": "deleted"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def _api_fact_delete(request: web.Request) -> web.Response:
    fact_id = request.match_info["fact_id"]
    # Remove from all tiers
    for tier in [_agent.fact_store.volatile, _agent.fact_store.working, _agent.fact_store.concrete]:
        tier[:] = [f for f in tier if f.id != fact_id]
    _agent.fact_store._archive_fact_by_id(fact_id) if hasattr(_agent.fact_store, '_archive_fact_by_id') else None
    _agent.fact_store.flush_to_db()
    # Also remove from DB
    _agent.fact_store._db.execute("DELETE FROM memory_facts WHERE id = ?", (fact_id,))
    _agent.fact_store._db.commit()
    return web.json_response({"result": f"Fact {fact_id} deleted"})


async def _api_goal_delete(request: web.Request) -> web.Response:
    goal_id = request.match_info["goal_id"]
    result = _agent.tools.goal_store.fail_goal(goal_id, "Deleted from dashboard")
    return web.json_response({"result": result})


async def _api_goal_pause(request: web.Request) -> web.Response:
    goal_id = request.match_info["goal_id"]
    result = _agent.tools.goal_store.pause_goal(goal_id)
    return web.json_response({"result": result})


async def _api_job_delete(request: web.Request) -> web.Response:
    job_id = request.match_info["job_id"]
    result = _agent.tools.scheduler.remove_job(job_id)
    return web.json_response({"result": "removed" if result else "not found"})


async def _api_model_switch(request: web.Request) -> web.Response:
    data = await request.json()
    model_name = data.get("model", "")
    session_id = data.get("session_id", "web:default")
    entry = _agent.router.set_active(session_id, model_name)
    if entry:
        return web.json_response({"result": f"Switched to {entry.name}", "model": entry.name})
    return web.json_response({"error": f"Unknown model: {model_name}"}, status=400)


# ── WebSocket Chat ───────────────────────────────────────────────────────────

async def _ws_ui_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    ws_id = f"web-{id(ws)}"
    _ui_clients[ws_id] = ws
    session_id = "web:default"

    # Register tool notify for this session
    async def tool_notify(msg: str) -> None:
        if not ws.closed:
            await ws.send_json({"type": "tool_status", "content": msg})

    _agent._tool_notify[session_id] = tool_notify

    # Register as channel (stable ID so reconnects don't create duplicates)
    _agent.tools.memory_store.register_channel(
        platform="web",
        platform_id="dashboard",
        session_id=session_id,
        name="Web UI",
        modality="text",
    )

    log.info("Web UI client connected: %s", ws_id)

    try:
        async for raw_msg in ws:
            if raw_msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(raw_msg.data)
                    if data.get("type") == "chat":
                        content = data.get("content", "")
                        if content:
                            async def _handle(ws_ref, msg, sid):
                                try:
                                    response = await _agent.handle_message(msg, session_id=sid)
                                    if not ws_ref.closed:
                                        await ws_ref.send_json({"type": "response", "content": response})
                                except Exception as e:
                                    if not ws_ref.closed:
                                        await ws_ref.send_json({"type": "error", "content": str(e)})
                            asyncio.create_task(_handle(ws, content, session_id))
                except json.JSONDecodeError:
                    pass
            elif raw_msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                break
    finally:
        _ui_clients.pop(ws_id, None)
        # Touch the channel so it stays fresh while connected, stale cleanup handles the rest
        _agent.tools.memory_store.touch_channel("web", "dashboard")
        log.info("Web UI client disconnected: %s", ws_id)

    return ws


# ── Static Files ─────────────────────────────────────────────────────────────

async def _serve_index(request: web.Request) -> web.Response:
    index_path = WEB_DIR / "index.html"
    if index_path.exists():
        return web.Response(text=index_path.read_text(encoding="utf-8"), content_type="text/html")
    return web.Response(text="Web UI not found", status=404)


async def _serve_static(request: web.Request) -> web.Response:
    path = request.match_info.get("path", "")
    file_path = WEB_DIR / path
    if not file_path.exists() or not file_path.is_file():
        return web.Response(text="Not found", status=404)

    content_types = {
        ".html": "text/html",
        ".css": "text/css",
        ".js": "application/javascript",
        ".png": "image/png",
        ".svg": "image/svg+xml",
        ".ico": "image/x-icon",
    }
    ct = content_types.get(file_path.suffix, "application/octet-stream")

    if ct.startswith("text") or ct == "application/javascript":
        return web.Response(text=file_path.read_text(encoding="utf-8"), content_type=ct)
    return web.Response(body=file_path.read_bytes(), content_type=ct)
