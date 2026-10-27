"""The dashboard's Huddles API: ``/huddle/account-api``.

The dashboard proxies ``/account/huddle/api`` here with the account cookie
(rook/remote/knowledge_web.py ``HuddleWeb``), with the same session, operator
and CSRF checks as Knowledge (rook/remote/account_api.py). GET returns the
CSRF token; every POST is ``{csrf, action, ...}``:

* ``list``: every huddle, with open question counts.
Rooms, cards and projects are named by id or speakable name.

* ``view`` {room | project | band, since_seq?}: the status strip, questions
  and messages; opening a project's or band's huddle makes it.
* ``send`` {room, text, mention?}: a plain message from the person.
* ``answer`` {question, text}: answer a pinned question (wakes the asker).

Chaired huddles (docs/design/huddles.md 5, 8):

* ``list`` {archived?}: archived ones too.
* ``start`` {title, roles?}: a new chaired huddle (roles: ids; none: the
  default roles; [] for a 1:1 with the house agent).
* ``send`` in a chaired huddle is a post: it starts a round.
* ``card`` {room, card, decision: approve|dismiss, reason?}: approving runs
  the card's call as this account, under the band's policy; a dismissal's
  reason goes back to the proposer in the room.
* ``archive`` {room, archived}.
* ``roles``: the roles to pick from, the default roles and the chair's
  identity.

Reading aloud (the Speak button on each message; huddle/speech.py):

* ``speak`` {room, seq | card, part?, plan?}: the message as speakable text,
  cut in ``parts`` sentence-sized chunks; ``mode`` hub with ``audio`` (base64
  WAV of chunk ``part``) in the speaking role's (or the house agent's) voice,
  else the band's default (``source`` role or default); ``mode`` browser with
  the whole ``text`` and a ``reason`` when the hub has no voice engine to
  speak with (the browser reads it). ``plan`` returns the voice choice and the
  chunks' ``chunks`` texts without audio, so the page can fetch them ahead and
  keep what it has played.
* ``voices``: the voice engine's voices and the band's default voice (the
  voice pickers on the Roles, Home agent and Voice pages); ``preview``
  {voice?}: a short sample in that voice (blank: the band's default).
* ``speak_devices``: workers a message may be played on; ``speak_on``
  {room, seq | card, worker}: ``voice.speak`` it there, in the same voice,
  one chunk per call in order (the device queues them); the first is
  waited for, the rest follow in the background.

The Roles page and the Memory page's role filter (huddles.md 8, 3.3) use the
``role.*`` caps through here, as this operator account (a band owner, so the
roles plugin's admin gate lets it change roles):

* ``role_list`` (roles, the presets by category, agent hosts, persona
  profiles), ``role_get`` {id}, ``role_history`` {id}, ``role_presets``
  {id?, category?, full?} (one preset's whole document with ``id``),
  ``role_memory`` {id, query?, limit?}, ``role_workspace`` {id, path?} (its
  workspace files, read-only: the list, or one file), ``role_sessions``
  {id} (the coding sessions it started);
* ``role_set`` {role, note?, expect_rev?, dry_run?}, ``role_delete`` {id},
  ``role_move`` {id, host}, ``role_memory_edit`` {id, op: remember|forget,
  text?, kind?, memory_id?}.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
from typing import Any, Callable

from starlette.routing import Route

from ....remote.account_api import account_api, json_error, json_reply
from . import speech
from .service import PERSON

PATH = "/huddle/account-api"
HUDDLE_ACTIONS = ("list", "view", "send", "answer", "start", "card", "archive", "roles", "pin",
                  "vote")
SPEECH_ACTIONS = ("speak", "voices", "preview", "speak_devices", "speak_on")
#: What a preview says when the hub cannot speak (the browser reads it).
PREVIEW_FALLBACK = "Hello. This is how this browser sounds; the hub has no voice engine now."
#: Longest a voice.speak on a device may take to be accepted.
SPEAK_ON_TIMEOUT = 20.0
log = logging.getLogger(__name__)
#: The house agent's front door (rook/hub/plugins/home/front.py; the Decisions page).
FRONT_ACTIONS = ("front_count", "front_view", "front_answer", "front_explain", "front_overturn",
                 "front_playbook_save")
WEAK_FRONT = ("This sign-in uses a password that is stored as a band secret, so agents could "
              "sign in as you with it. It can't say yes to an action. Answer on your phone's "
              "card, or change the password (or remove that secret) and sign in again.")
ROLE_ACTIONS = ("role_list", "role_get", "role_history", "role_presets", "role_memory",
                "role_workspace", "role_sessions", "role_set", "role_delete", "role_move",
                "role_memory_edit")


#: "Play on" deliveries still sending their later chunks.
_background: set = set()


async def _speak_rest(say: Callable, pieces: list[str], worker: str) -> None:
    """Send a message's later chunks to a device in order. A chunk that fails
    twice is skipped; two skipped in a row (the device is gone) end it."""
    skipped = 0
    for i, piece in enumerate(pieces, 2):
        for attempt in (1, 2):
            try:
                reply = await say(piece)
            except Exception as e:  # noqa: BLE001 - logged, then retried or skipped
                reply = {"ok": False, "error": str(e)[:160]}
            if reply.get("ok"):
                skipped = 0
                break
            log.warning("huddle: play on %s: chunk %d failed (try %d): %s",
                        worker, i, attempt, reply.get("error"))
        else:
            skipped += 1
            if skipped >= 2:
                return


def _role_args(action: str, data: dict) -> dict:
    """The cap arguments a role action may pass (nothing else gets through)."""
    rid = str(data.get("id") or "")
    if action in ("role_get", "role_history"):
        return {"id": rid}
    if action == "role_presets":
        return {"id": rid, "category": str(data.get("category") or ""),
                "full": bool(data.get("full"))}
    if action == "role_workspace":
        return {"id": rid, "path": str(data.get("path") or "")[:400]}
    if action == "role_sessions":
        return {"role": rid, "limit": 20}
    if action == "role_memory":
        return {"id": rid, "query": str(data.get("query") or ""),
                "limit": max(1, min(int(data.get("limit") or 50), 200))}
    if action == "role_set":
        out: dict = {"role": data.get("role"), "note": str(data.get("note") or "")[:200],
                     "dry_run": bool(data.get("dry_run"))}
        if data.get("expect_rev") is not None:
            out["expect_rev"] = int(data["expect_rev"])
        return out
    if action == "role_delete":
        return {"id": rid, "note": str(data.get("note") or "")[:200]}
    if action == "role_move":
        return {"id": rid, "host": str(data.get("host") or ""),
                "note": str(data.get("note") or "")[:200]}
    if action == "role_memory_edit":
        return {"id": rid, "action": str(data.get("op") or ""), "text": str(data.get("text") or ""),
                "kind": str(data.get("kind") or "fact"),
                "memory_id": str(data.get("memory_id") or "")}
    return {}


def _account_principal(node: Any, user: dict) -> Any:
    """This dashboard account as a policy principal: an owner when it is the
    operator or access.db says so (access v2), else a member."""
    from ...access import AccessStore
    from ...authz import principal_for_user
    owner = bool(user.get("admin"))
    store = getattr(getattr(getattr(node, "client", None), "authz", None), "access", None)
    if not owner and isinstance(store, AccessStore):
        with contextlib.suppress(Exception):
            owner = store.is_owner(str(user.get("id") or ""))
    return principal_for_user(user, owner)


def routes(get_node: Callable[[], Any], accounts):
    """``get_node()`` returns the live hub node (or None)."""

    def service():
        node = get_node()
        plugin = node.plugin("huddle") if node is not None else None
        return getattr(plugin, "service", None)

    def plugin(ns: str):
        node = get_node()
        return node.plugin(ns) if node is not None else None

    async def role_action(action: str, data: dict, user: dict) -> Any:
        """A ``role.*`` cap as this operator account: its principal for the
        admin gate and the policy, its name as the actor in the role's history."""
        from ...authz import current_principal, principal_for_user
        node = get_node()
        rp = plugin("role")
        if node is None or rp is None:
            raise LookupError("Roles are off on this hub (the roles plugin is not loaded).")
        cap = "role." + action[len("role_"):]
        principal = principal_for_user(user, True)
        tok = current_principal.set(principal)
        try:
            result = await node.invoke(cap, _role_args(action, data),
                                       "human:" + str(user.get("username") or user["id"]))
        finally:
            current_principal.reset(tok)
        if action == "role_list":
            home = plugin("home")
            hosts = []
            with contextlib.suppress(Exception):
                hosts = home.agents.hosts() if home is not None else []
            personas = []
            pp = plugin("persona")
            with contextlib.suppress(Exception):
                personas = [{"id": p["id"], "name": p.get("name") or p["id"]}
                            for p in pp.store.profiles()] if pp is not None else []
            result = {**result, "preset_categories": rp.presets()["categories"], "hosts": hosts,
                      "personas": personas,
                      "chair": home.identity if home is not None else None}
        return result

    async def front_action(action: str, data: dict, user: dict, request) -> Any:
        """The Decisions page: the house agent's asks (Yes / No / Explain),
        the digest with Overturn, and the playbook, as this person."""
        home = plugin("home")
        front = getattr(home, "front", None)
        if home is None or front is None or not home.enabled():
            raise LookupError("The house agent is off on this hub (Intelligence sources > Home agent).")
        who = str(user.get("name") or user.get("username") or user["id"])
        if action == "front_count":
            return {"asks": len(front.store.asks("open"))}
        if action == "front_view":
            return {**front.view(), "front_door": home.front_door()}
        if action == "front_explain":
            return front.explain(str(data.get("ask") or ""), bool(data.get("more")))
        if action == "front_answer":
            a = front.store.find_ask(str(data.get("ask") or ""))
            decision = str(data.get("decision") or "")
            if a is not None and a.get("action") and decision == "yes":
                weak = True
                with contextlib.suppress(Exception):
                    from ....remote.account_web import COOKIE
                    weak = bool(accounts.session_weak(request.cookies.get(COOKIE, "")))
                if weak:
                    raise PermissionError(WEAK_FRONT)
            return await front.answer(str(data.get("ask") or ""), decision, by=who,
                                      quote=f"{'Yes' if decision == 'yes' else 'No'} on the "
                                            "dashboard's Decisions page",
                                      reason=str(data.get("reason") or "")[:500],
                                      principal=_account_principal(get_node(), user))
        if action == "front_overturn":
            return front.overturn(str(data.get("decision") or ""), by=who,
                                  words=str(data.get("words") or "Overturned on the dashboard"),
                                  kind="person")
        if action == "front_playbook_save":
            from ..home.playbook import PlaybookError
            try:
                out = front.playbook.save_text(str(data.get("text") or ""), who=who,
                                               words=str(data.get("words") or ""))
            except PlaybookError as e:
                raise ValueError(str(e)) from None
            return {**out, "playbook": front.playbook.public()}
        raise ValueError("unknown front action")

    # -- reading aloud ------------------------------------------------------------
    def name_for(rid: str) -> str | None:
        """A record id's three-word name: huddles, cards and questions, then
        tasks and projects, then jobs and runs."""
        svc = service()
        if svc is not None:
            with contextlib.suppress(Exception):
                if n := svc.store.name_for_id(rid):
                    return n
        kp = plugin("knowledge")
        store = getattr(getattr(kp, "service", None), "store", None)
        if store is not None:
            from ....core import names
            with contextlib.suppress(Exception), store.db(write=False) as db:
                if n := names.name_for_id(db, rid):
                    return n
        jp = plugin("jobs")
        js = getattr(jp, "store", None)
        if js is not None:
            for kind in ("job", "run"):
                with contextlib.suppress(Exception):
                    if n := js.name_of(kind, rid):
                        return n
        return None

    def role_voice(sender: str) -> tuple[str, str]:
        """``(role id, its voice)`` of a role sender (``role:qa``), ``("", the
        house agent's voice)`` for the house agent (the chair), else blanks."""
        if not sender.startswith("role:"):
            hp = plugin("home")
            if hp is not None and sender == getattr(hp, "identity", None):
                with contextlib.suppress(Exception):
                    return "", str(hp.settings.get("voice") or "").strip()
            return "", ""
        rid, rp = sender[5:], plugin("role")
        doc = None
        with contextlib.suppress(Exception):
            doc = rp.store.get(rid) if rp is not None else None
        return rid, str((doc or {}).get("voice") or "")

    async def engine() -> tuple[Any, str, dict | None]:
        """``(voice plugin, why it cannot speak, its voices)``."""
        vp = plugin("voice")
        if vp is None or not hasattr(vp, "synthesize"):
            return None, "this hub has no voice engine (the voice plugin is not running)", None
        why = vp.speech_unavailable()
        if why:
            return vp, why, None
        try:
            return vp, "", await vp.voices()
        except Exception as e:  # noqa: BLE001 - speaking may still work
            return vp, "", {"voices": None, "default": "", "error": str(e)[:200]}

    def source(svc, data: dict) -> tuple[str, dict]:
        room = svc.room_id(str(data.get("room") or ""), write=False)[0]
        return room, svc.spoken(room, seq=int(data.get("seq") or 0), card=str(data.get("card") or ""))

    def protected() -> set[str]:
        rp = plugin("role")
        fn = getattr(rp, "protected", None)
        with contextlib.suppress(Exception):
            return {str(x).lower() for x in fn()} if fn is not None else set()
        return set()

    def devices() -> list[dict]:
        """Online workers the person may play a message on: ``huddle.speak_devices``
        (blank: every one with voice.speak), never a protected device, and none
        when the hub cannot tell family devices apart (no protected list)."""
        node = get_node()
        client = getattr(node, "client", None)
        roster = dict(getattr(client, "workers", {}) or {}) if client is not None else {}
        hp = plugin("huddle")
        listed = [str(x).strip().lower() for x in
                  ((hp.settings.get("speak_devices") if hp is not None else None) or []) if str(x).strip()]
        blocked = protected()
        if not listed and not blocked:
            return []
        out = []
        for wid, w in roster.items():
            name = str(w.get("name") or wid)
            if "voice.speak" not in (w.get("caps") or []) or name.lower() in blocked:
                continue
            if listed and name.lower() not in listed and wid.lower() not in listed:
                continue
            out.append({"worker": name, "id": wid, "description": str(w.get("description") or "")})
        return sorted(out, key=lambda d: d["worker"].lower())

    async def speech_action(action: str, data: dict, user: dict) -> Any:
        if action == "speak_devices":
            return {"devices": devices()}
        if action == "voices":
            vp, why, voices = await engine()
            return {"available": not why, "reason": why, "voices": (voices or {}).get("voices") or [],
                    "default": (voices or {}).get("default") or "",
                    "engine_default": (voices or {}).get("engine_default") or "",
                    "catalog": (voices or {}).get("catalog") or [],
                    "error": (voices or {}).get("error") or ""}
        if action == "preview":
            vp, why, _ = await engine()
            if why:
                return {"mode": "browser", "reason": why, "text": PREVIEW_FALLBACK}
            voice = str(data.get("voice") or "").strip()
            if not speech.VOICE_ID.match(voice):
                raise ValueError("voice: a voice id such as af_heart")
            try:
                return {"mode": "hub", **await vp.preview(voice)}
            except Exception as e:  # noqa: BLE001 - the picker says why
                return {"mode": "browser", "reason": f"the voice engine failed: {str(e)[:160]}",
                        "text": PREVIEW_FALLBACK}
        svc = service()
        if svc is None:
            raise LookupError("Huddles are off on this hub.")
        room, src = source(svc, data)
        text = speech.speakable(src["text"], name_for)
        pieces = speech.parts(text)
        role, rvoice = role_voice(src["sender"])
        if action == "speak_on":
            want = str(data.get("worker") or "").strip().lower()
            found = next((d for d in devices() if want in (d["worker"].lower(), d["id"].lower())), None)
            if found is None:
                raise PermissionError("That device is not offered for playing messages "
                                      "(huddle.speak_devices; protected devices never are).")
            if not text:
                raise ValueError("nothing to say in that message")
            client = getattr(get_node(), "client", None)
            if client is None:
                raise LookupError("This hub has no band connection.")
            from ...authz import principal_for_user
            identity = "human:" + str(user.get("username") or user["id"])
            principal = principal_for_user(user, True)

            async def say(piece: str) -> dict:
                # Chunks queue on the device (interrupt off); chat off, or the
                # device's chat would get the message in pieces.
                args: dict = {"text": piece, "wait": False, "chat": False}
                if rvoice:
                    args["voice"] = rvoice
                return await client.call(cap="voice.speak", args=args, target=found["id"],
                                         timeout=SPEAK_ON_TIMEOUT, identity=identity,
                                         principal=principal)
            reply = await say(pieces[0])
            if not reply.get("ok"):
                raise ValueError(f"{found['worker']}: {reply.get('error') or 'voice.speak failed'}")
            if len(pieces) > 1:
                task = asyncio.create_task(_speak_rest(say, pieces[1:], found["worker"]))
                _background.add(task)
                task.add_done_callback(_background.discard)
            return {"worker": found["worker"], "voice": rvoice, "parts": len(pieces)}
        vp, why, voices = await engine()
        known = (voices or {}).get("voices")
        choice = speech.choose_voice(rvoice, unavailable=why, voices=known,
                                     default=(voices or {}).get("default") or "")
        part = max(0, int(data.get("part") or 0))
        out: dict = {**choice, "role": role, "parts": len(pieces), "part": part}
        if choice["mode"] == "browser" or not pieces:
            return {**out, "text": text}
        if data.get("plan"):
            return {**out, "chunks": pieces}
        if part >= len(pieces):
            raise ValueError(f"part: 0 to {len(pieces) - 1}")
        audio, failed = None, ""
        for voice in ([choice["voice"], ""] if choice["voice"] else [""]):
            try:
                audio = await vp.synthesize(pieces[part], voice)
            except Exception as e:  # noqa: BLE001 - next the default, then the browser
                failed = failed or str(e)[:160]
                continue
            if voice != choice["voice"]:     # the role's voice failed: the band's default
                out.update(voice="", source="default", label=(voices or {}).get("default") or "",
                           reason=f"the role's voice failed: {failed}")
            break
        if audio is None:
            return {**out, "mode": "browser", "source": "browser", "voice": "",
                    "reason": f"the voice engine failed: {failed}", "text": text}
        return {**out, "audio": base64.b64encode(audio).decode(), "mime": "audio/wav"}

    @account_api(accounts, sign_in="Sign in to see huddles.",
                 operator_only="Huddles need the operator account.")
    async def api(request, user, data):
        if data is None:
            return json_reply({"csrf": user["csrf"], "you": PERSON,
                               "huddles": service() is not None,
                               "front": getattr(plugin("home"), "front", None) is not None,
                               "roles": plugin("role") is not None})
        action = data.get("action")
        if action in SPEECH_ACTIONS:
            try:
                return json_reply({"ok": True, "result": await speech_action(action, data, user)})
            except PermissionError as error:
                return json_error(str(error), 403)
            except LookupError as error:
                return json_error(str(error), 503)
            except (ValueError, KeyError, TypeError) as error:
                return json_error(str(error), 400)
        if action in FRONT_ACTIONS:
            try:
                return json_reply({"ok": True, "result": await front_action(action, data, user,
                                                                            request)})
            except PermissionError as error:
                return json_error(str(error), 403)
            except LookupError as error:
                return json_error(str(error), 503)
            except (ValueError, KeyError, TypeError) as error:
                return json_error(str(error), 400)
        if action in ROLE_ACTIONS:
            try:
                return json_reply({"ok": True, "result": await role_action(action, data, user)})
            except PermissionError as error:
                return json_error(str(error), 403)
            except LookupError as error:
                return json_error(str(error), 503)
            except (ValueError, KeyError, TypeError) as error:
                return json_error(str(error), 400)
        svc = service()
        if svc is None:
            return json_error("Huddles are off on this hub.", 503)
        where = {k: (str(data[k]) if data.get(k) else None) for k in ("room", "project", "band")}
        try:
            if action == "list":
                result = {"huddles": svc.huddles(archived=bool(data.get("archived")))}
            elif action == "start":
                roles = data.get("roles")
                result = await svc.start(PERSON, str(data.get("title") or ""),
                                         roles if isinstance(roles, list) else None)
            elif action == "card":
                from ...authz import principal_for_user
                result = await svc.card(str(data.get("room") or ""), str(data.get("card") or ""),
                                        str(data.get("decision") or ""), "human:" + str(user["id"]),
                                        principal_for_user(user, True),
                                        reason=str(data.get("reason") or "")[:500])
            elif action == "archive":
                result = svc.archive(str(data.get("room") or ""), bool(data.get("archived", True)),
                                     "human:" + str(user.get("username") or user["id"]))
            elif action == "roles":
                rp, home, hp = plugin("role"), plugin("home"), plugin("huddle")
                defaults = list((hp.settings.get("default_roles") if hp is not None else None)
                                or [])
                result = {"roles": rp.store.list() if rp is not None else [],
                          "default_roles": defaults,
                          "chair": home.identity if home is not None else None,
                          "chair_on": bool(home is not None and home.enabled())}
            elif action == "view":
                result = await svc.view(PERSON, since_seq=int(data.get("since_seq") or 0),
                                        limit=min(int(data.get("limit") or 100), 300),
                                        create=True, targets=True, **where)
            elif action == "send":
                mention = data.get("mention") or []
                if isinstance(mention, str):
                    mention = [m.strip() for m in mention.split(",") if m.strip()]
                if where["room"]:
                    where["room"] = svc.room_id(where["room"])[0]
                if where["room"] and svc.store.chaired(where["room"]):
                    result = await svc.post(PERSON, where["room"], str(data.get("text") or ""),
                                            mention)
                else:
                    result = await svc.say(PERSON, str(data.get("text") or ""), mention, **where)
            elif action in ("pin", "vote"):
                # The conference table, as the person (table.py).
                if svc.table is None:
                    raise ValueError("the conference table is off on this hub")
                room = svc.room_id(str(data.get("room") or ""))[0]
                body = {k: v for k, v in (data.get("data") or {}).items() if v not in (None, "")}
                from .table import TableError
                try:
                    result = await (svc.table.pin(room, PERSON, body) if action == "pin"
                                    else svc.table.call(room, PERSON, body))
                except TableError as e:
                    raise ValueError(str(e)) from None
            elif action == "answer":
                result = await svc.answer(int(data.get("question") or 0), str(data.get("text") or ""),
                                          "human:" + str(user["id"]), str(user.get("name") or user["id"]))
            else:
                return json_error("action must be one of " + ", ".join(HUDDLE_ACTIONS + SPEECH_ACTIONS
                                                                       + ROLE_ACTIONS),
                                  400)
        except PermissionError as error:
            return json_error(str(error), 403)
        except (ValueError, KeyError, TypeError) as error:
            if getattr(error, "candidates", None):   # a name that was not exactly one record's
                return json_reply(error.payload(), 400)
            return json_error(str(error), 400)
        return json_reply({"ok": True, "result": result})

    return [Route(PATH, api, methods=["GET", "POST"])]
