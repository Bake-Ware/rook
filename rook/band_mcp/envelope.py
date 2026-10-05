"""Compact MCP reply envelope and tool-listing slimming.

Every character a tool returns or advertises is paid for in agent context, so
this module owns the MCP-facing shape of things:

* ``dumps``: JSON without indentation or ASCII escaping.
* ``install``: re-serialises any indented JSON a tool returns, compactly, and
  drops FastMCP's structured copy of string results (the same payload sent a
  second time as an escaped string).
* ``slim_tool``: removes the output schema and pydantic noise (titles,
  ``anyOf [X, null]``) from a tool's advertised input schema. Validation still
  uses the tool's own argument model, so accepted input does not change.
* ``Notices``: per-MCP-session memory so ``_task`` and ``_unread_chat`` ride a
  reply only when they are new or changed.
* ``add_notice``: adds a key (``_hygiene``) to a reply Rook built, after the tool ran.
* ``call_reply`` / ``call_text``: the ``rook_call`` reply, compact or plain text.

``ROOK_MCP_ENVELOPE=legacy`` restores the pre-beta ``rook_call`` shape
(indented, hex ``from``, ``_journal_id``, notices on every reply) for clients
that depended on it. Band (worker) wire messages are untouched either way.
"""
from __future__ import annotations

import json
import logging
import os
from collections import OrderedDict

log = logging.getLogger("rook.band_mcp.envelope")


def legacy() -> bool:
    return os.environ.get("ROOK_MCP_ENVELOPE", "").strip().lower() == "legacy"


def dumps(obj) -> str:
    if legacy():
        return json.dumps(obj, indent=2, default=str)
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=str)


# -- tool listing ------------------------------------------------------------

def _slim_schema(node):
    """Drop pydantic ``title``s and collapse ``anyOf [X, null]`` to ``X``."""
    if isinstance(node, list):
        return [_slim_schema(x) for x in node]
    if not isinstance(node, dict):
        return node
    out = {}
    for k, v in node.items():
        if k == "title" and isinstance(v, str):
            continue  # a schema title, never a property (those live under "properties")
        if k == "properties" and isinstance(v, dict):
            out[k] = {name: _slim_schema(p) for name, p in v.items()}
            for p in out[k].values():
                # false / "" / 0 go without saying for an optional flag, string or cursor
                if isinstance(p, dict) and "default" in p and p["default"] in (False, "", 0) \
                        and not isinstance(p["default"], float):
                    del p["default"]
        else:
            out[k] = _slim_schema(v)
    if "anyOf" in out:
        alts = [a for a in out["anyOf"] if a != {"type": "null"}]
        if len(alts) == 1:
            del out["anyOf"]
            out = {**alts[0], **out}
        else:
            out["anyOf"] = alts
        if out.get("default", 0) is None:
            del out["default"]  # optional already: it isn't in "required"
    if out.get("type") == "object" and out.get("additionalProperties") is True:
        del out["additionalProperties"]
    if out.get("type") == "array" and out.get("items") == {}:
        del out["items"]
    return out


def slim_tool(tool) -> None:
    """Advertise ``tool`` compactly: no output schema, lean input schema."""
    try:
        fm = tool.fn_metadata
        if fm.output_schema is not None:
            tool.fn_metadata = fm.model_copy(update={"output_schema": None, "output_model": None,
                                                     "wrap_output": False})
        tool.parameters = _slim_schema(tool.parameters)
    except Exception:  # noqa: BLE001 — a fat listing beats a broken one
        log.exception("could not slim tool %s", getattr(tool, "name", "?"))


def install(mcp) -> None:
    """Compact every tool's JSON text result on the way out."""
    run = mcp._tool_manager.call_tool

    async def compact_call_tool(name, arguments, context=None, convert_result=False):
        result = await run(name, arguments, context=context, convert_result=convert_result)
        if legacy():
            return result
        try:
            items = result[0] if isinstance(result, tuple) else result
            if isinstance(items, list):
                for item in items:
                    text = getattr(item, "text", None)
                    if isinstance(text, str) and "\n " in text and text[:1] in "[{":
                        item.text = dumps(json.loads(text))
        except (ValueError, TypeError):
            pass
        except Exception:  # noqa: BLE001 — never break a reply over formatting
            log.exception("compacting %s reply failed", name)
        return result

    mcp._tool_manager.call_tool = compact_call_tool


# -- per-session notices -----------------------------------------------------

class Notices:
    """What each MCP session has already been told, so repeats are dropped."""

    def __init__(self, cap: int = 5000) -> None:
        self._seen: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._cap = cap

    def fresh(self, session: str, kind: str, value) -> bool:
        """True if ``value`` differs from what ``session`` last saw for ``kind``."""
        if legacy() or not session:
            return True
        key, sig = (session, kind), json.dumps(value, sort_keys=True, default=str)
        if self._seen.get(key) == sig:
            self._seen.move_to_end(key)
            return False
        self._seen[key] = sig
        self._seen.move_to_end(key)
        while len(self._seen) > self._cap:
            self._seen.popitem(last=False)
        return True


def add_notice(result, key: str, value):
    """Add ``key: value`` to a tool result after the tool ran (the hygiene
    piggyback). Returns ``(result, attached)``.

    Only for replies Rook builds itself as one JSON object (the caller picks
    the tools; see server._HYGIENE_TOOLS): the key is merged into that
    object. Anything else (a JSON array, plain text, a worker's output) is
    left untouched and ``attached`` is False, so the caller keeps the notice
    for a later reply instead of editing data it does not own. ``rook_call``
    adds its notices itself (``call_reply`` / ``call_text``). Accepts the raw
    string a tool returns or FastMCP's converted content list / tuple."""
    def edit(text: str):
        if text[:1] != "{":
            return None
        try:
            obj = json.loads(text)
        except ValueError:
            return None
        if not isinstance(obj, dict) or key in obj:
            return None
        obj[key] = value
        return dumps(obj)
    if isinstance(result, str):
        out = edit(result)
        return (result, False) if out is None else (out, True)
    items = result[0] if isinstance(result, tuple) else result
    if isinstance(items, list):
        texts = [item for item in items if isinstance(getattr(item, "text", None), str)]
        if len(texts) == 1:
            out = edit(texts[0].text)
            if out is not None:
                texts[0].text = out
                return result, True
    return result, False


# -- rook_call replies -------------------------------------------------------

_SHELL_KEYS = {"ok", "code", "stdout", "stderr"}


def compact_result(cap: str, result):
    """shell.exec-shaped results lose empty streams and the derivable ``ok``."""
    if (isinstance(result, dict) and "code" in result and set(result) <= _SHELL_KEYS
            and isinstance(result.get("code"), int)):
        out = {"code": result["code"]}
        if result.get("ok") != (result["code"] == 0):
            out["ok"] = result.get("ok")
        for stream in ("stdout", "stderr"):
            if result.get(stream):
                out[stream] = result[stream]
        return out
    return result


def call_reply(reply: dict, cid: str, worker_name: str | None, cap: str,
               notices: dict) -> dict:
    """The compact ``rook_call`` reply: ``{ok, id, from, result|error, notices…}``.

    ``id`` is the journal id (the band message id; ``rook_journal(call_id=id)``
    returns the stored reply). ``from`` is the worker's name.
    """
    if legacy():
        return {**reply, "_journal_id": cid, **notices}
    out = {"ok": reply.get("ok", False), "id": reply.get("id") or cid,
           "from": worker_name or reply.get("from")}
    for k, v in reply.items():
        if k in ("ok", "id", "from"):
            continue
        out[k] = compact_result(cap, v) if k == "result" else v
    out.update(notices)
    return out


def call_text(reply: dict, cap: str, notices: dict) -> str | None:
    """Plain-text form of a successful reply, or None when it isn't one.

    shell.exec: stdout, then ``[stderr]`` and ``[exit N]`` only when non-empty
    or non-zero. Other caps: a string result as-is, anything else as compact
    JSON. Notices follow on a last ``[rook] {...}`` line.
    """
    if not reply.get("ok"):
        return None
    res = reply.get("result")
    if isinstance(res, dict) and isinstance(res.get("code"), int) and ("stdout" in res or "stderr" in res):
        parts = [res.get("stdout") or ""]
        if res.get("stderr"):
            parts.append("[stderr]\n" + res["stderr"])
        if res["code"] != 0:
            parts.append(f"[exit {res['code']}]")
        body = "\n".join(p.rstrip("\n") for p in parts if p)
        if not body:
            body = "[exit 0, no output]"
    elif isinstance(res, dict) and res.get("ok") is False and "error" in res:
        return None  # a handler-level failure reads better as the JSON envelope
    elif isinstance(res, str):
        body = res
    else:
        body = json.dumps(res, separators=(",", ":"), ensure_ascii=False, default=str)
    if notices:
        body += "\n[rook] " + json.dumps(notices, separators=(",", ":"), ensure_ascii=False, default=str)
    return body


# -- rosters -----------------------------------------------------------------

def fields_arg(fields) -> list[str] | None:
    """``fields`` as list or comma string; None when not given."""
    if fields is None or fields == "":
        return None
    if isinstance(fields, str):
        return [f.strip() for f in fields.split(",") if f.strip()]
    return [str(f).strip() for f in fields if str(f).strip()]


def describe_compact(described: dict, prefix: str = "") -> dict:
    """``caps.describe`` output as ``{cap: "sig — doc"}``.

    ``sig`` is Python-like: ``name: type`` for required args, ``name: type =
    default`` for optional ones (``| None`` dropped when the default is None).
    """
    out = {}
    for cap, spec in sorted(described.items()):
        if not cap.startswith(prefix):
            continue
        if not isinstance(spec, dict):
            out[cap] = spec
            continue
        params = []
        for p in spec.get("params") or []:
            if not isinstance(p, dict):
                continue
            typ = (p.get("type") or "").replace("typing.", "")
            default = p.get("default")
            if not p.get("required") and default is None and typ.endswith(" | None"):
                typ = typ[:-7]
            s = p.get("name", "?") + (f": {typ}" if typ else "")
            if not p.get("required"):
                s += " = " + json.dumps(default, ensure_ascii=False) if default is not None else " = None"
            params.append(s)
        doc = spec.get("doc") or ""
        out[cap] = f"({', '.join(params)})" + (f" — {doc}" if doc else "")
    return out
