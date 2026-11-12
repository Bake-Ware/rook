"""Reading the journal (L2): filter and search, newest first."""

import datetime
import json

from .errors import HubError


def iso(at):
    """A journal or record time (Unix seconds) as ISO 8601 UTC."""
    return None if at is None else datetime.datetime.fromtimestamp(at, datetime.timezone.utc).isoformat(
        timespec="seconds")


def _seconds(text, name):
    try:
        moment = datetime.datetime.fromisoformat(text)
    except (TypeError, ValueError):
        raise HubError("bad_message", f"{name} {text!r} is not an ISO 8601 time.",
                       f"Give {name} like 2026-11-12T17:00:00Z.") from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment.timestamp()


def read(hub, worker=None, cap=None, user=None, kind=None, since=None, until=None, text=None, limit=50):
    """Journal entries matching every filter given, newest first, at most `limit`.
    `worker` and `user` take a GUID or moniker (`hub` for the hub's own caps);
    `text` is found case-insensitively in the why, the cap and the result."""
    where, params = [], []
    with hub._lock:
        if worker is not None:
            where.append("j.worker = ?")
            params.append(worker if worker == "hub" else hub._resolve(worker)["guid"])
        if user is not None:
            where.append("j.user = ?")
            params.append(hub.auth._user(user)["guid"])
        for column, value in (("cap", cap), ("kind", kind)):
            if value is not None:
                where.append(f"j.{column} = ?")
                params.append(value)
        if since is not None:
            where.append("j.at >= ?")
            params.append(_seconds(since, "since"))
        if until is not None:
            where.append("j.at < ?")
            params.append(_seconds(until, "until"))
        if text:
            where.append("instr(lower(coalesce(j.why, '') || ' ' || coalesce(j.cap, '') || ' ' || j.result), lower(?)) > 0")
            params.append(text)
        rows = hub._db.execute(
            "SELECT j.*, u.moniker AS user_moniker, t.moniker AS token_moniker, w.moniker AS worker_moniker "
            "FROM journal j LEFT JOIN monikers u ON u.guid = j.user LEFT JOIN monikers t ON t.guid = j.token "
            "LEFT JOIN monikers w ON w.guid = j.worker "
            f"{'WHERE ' + ' AND '.join(where) if where else ''} ORDER BY j.id DESC LIMIT ?",
            (*params, limit)).fetchall()
    return [{"id": row["id"], "at": iso(row["at"]), "kind": row["kind"],
             "user": row["user_moniker"], "token": row["token_moniker"],
             "worker": row["worker_moniker"] or row["worker"], "cap": row["cap"], "why": row["why"],
             "result": json.loads(row["result"])} for row in rows]
