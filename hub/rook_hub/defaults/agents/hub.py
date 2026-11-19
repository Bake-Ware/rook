"""The house agent, on the hub (brief HA2): 0.5's agents runtime (cbe86a47:
rook/plugins/agents/scripts/agents.py and plugin.yaml) ported onto core's plugin format.

One agent lives on every hub: "house" (identity agent:house), from persona `house`
(personas/house.md, 0.5's words). Its name is the setting `house_name` (blank: "Rookie"). Like
the system owner it is the hub's own: its `agent` record is made at the first load, owned by the
system owner.

**Talking to it**: `agent.chat.send {agent, text, wait}` and `agent.chat.read {agent, after,
limit, wait, party}`, with 0.5's arguments and replies. Each person has one conversation with
it, kept for good: a stream the plugin opens at the hub ("house with person:<name>") whose
entries are 0.5's events ({at, type, by, data}; seq = position + 1): `input` (the message, as the
person sent it), `turn` (role prompt, then role reply) and `status` (an error). Its `agent-session`
record keeps the stream and the engine's own session id, which every turn resumes.

**A turn** runs on the hub, one at a time, in the order sent (Call.later, key "house"):
1. its text is "Message from person:<name>:" and the message, plus each skill carried now
   (Plugin.carry; first-run while setup is unfinished, brief HA3);
2. its system prompt is the agent's first line, the persona and its skills (and, once the memory
   slice is built, its memory: `memory_parts`);
3. a door token is minted for it with token.create, as the person it answers (T5), and revoked
   when the turn ends, as 0.5's turn token ended with its turn;
4. the engine is the cap the setting `backend` names (0.5's agent.backend: agents-claude-code.turn),
   called as that person on the worker the setting `worker` names (blank: the one online worker
   that announces it), with 0.5's arguments: prompt, system, session, model, cwd, door. The call
   gets a stream at once, named in the prompt event, so whoever closes it stops the turn (brief
   59). Nothing else stops it: a turn has no timeout.

Any job can send it a message (agent.chat.send as the job's call), and so can anyone at the door,
and so can the agent itself from inside a turn: that message is queued and answered after the
turn, and the send answers {agent, stream, seq} at once whatever `wait` says. Nothing is refused.

Other hub modules beside this one named `hub_*.py` are loaded in name order and their
`setup(plugin)` called with the same plugin (briefs HA1 and HA3 add theirs: agent.setup,
agent.look, the first-run skill); agent.site takes `look_state(call)` and `setup_state(call)`
from whichever of them defines them.
"""

import asyncio
import base64
import importlib.util
import json
import logging
import pathlib
import time

from rook_hub.library import Error

log = logging.getLogger("rook_hub.plugins.agents")

HERE = pathlib.Path(__file__).resolve().parent
HOUSE = "house"                   # the house agent's name: its identity is agent:house
HOUSE_NAME = "Rookie"             # what it is called while house_name is blank (0.5)
ENGINE = "agents-claude-code.turn"
AGENT, SESSION = "agent", "agent-session"     # the record types this plugin owns (K2, K5)
PERSONA = {
    "name": HOUSE, "persona": HOUSE, "title": "House agent",
    "summary": "The person's personal assistant and guardian of the house: knows how and when to reach them.",
    "skills": ["first-run", "reach-the-user", "presence", "attention", "hub-pages", "consult", "notepad",
               "housekeeping", "plugins"],
    "backend": "default", "model": {}, "cwd": "",
}
_turning = set()                  # the door tokens (JWTs) of the turns running now
_parts = []                       # the hub_*.py modules loaded beside this one


def setup(plugin):
    plugin.record_type(AGENT)
    plugin.record_type(SESSION)
    plugin.setting("house_name", "The house agent's name: what it calls itself and what the hub site calls it "
                                 "(first-run setup asks it); blank: Rookie. Its identity stays agent:house.")
    plugin.setting("dock", "The agent in the hub site's chat dock; blank: no dock.", default=HOUSE)
    plugin.setting("backend", "The engine cap an agent with backend default uses (0.5's agent.backend).",
                   default=ENGINE)
    plugin.setting("worker", "The worker the engine runs on (moniker, name or GUID); blank: the one online "
                             "worker that announces the engine cap.")
    plugin.on_load(made)
    for name, (handler, description, schema) in CAPS.items():
        plugin.cap(name, description, schema)(handler)
    _parts.clear()
    for path in sorted(HERE.glob("hub_*.py")):
        spec = importlib.util.spec_from_file_location(f"{__name__}_{path.stem}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.setup(plugin)
        _parts.append(module)


# ------------------------------------------------------------------ the agent and its sessions

def house(call):
    """The house agent's record, made at the first load (`made`)."""
    for record in call.search(AGENT, limit=-1):
        if record["body"].get("name") == HOUSE:
            return record
    raise Error("unknown_agent", "This hub has no house agent yet.", "Start the hub again; it makes it.")


def title(call, record):
    """What it is called: the house agent's name is the house_name setting (0.5)."""
    if record["body"].get("name") == HOUSE:
        return str(call.settings["house_name"] or HOUSE_NAME)
    return record["title"] or record["body"]["name"]


def _agent(call, args):
    name = args.get("agent")
    if not isinstance(name, str) or not name:
        raise Error("bad_message", "agent is required: the agent's name, such as house.",
                    "List the agents with agent.list.")
    if name != HOUSE:
        raise Error("unknown_agent", f"No agent {name}.", "This hub has one agent, house; agent.list shows it.")
    return house(call)


def _sessions(call):
    return [r for r in call.search(SESSION, limit=-1) if r["body"].get("agent") == HOUSE]


def session(call, person, create):
    """The agent's conversation with `person` ({guid, moniker, name}): its record, made if `create`."""
    for record in _sessions(call):
        if record["body"].get("user") == person["guid"]:
            return record
    if not create:
        return None
    party = f"person:{person['name']}"
    stream = call.open_stream(f"{HOUSE} with {party}",
                              f"The house agent's conversation with {person['name']} ({person['moniker']}), kept "
                              "for good. Read it here; add to it with agent.chat.send.")
    return call.create(SESSION, f"{HOUSE} with {party}", f"The house agent's conversation with {person['name']}.",
                       body={"agent": HOUSE, "user": person["guid"], "party": party, "stream": stream["guid"],
                             "harness": None, "harness_backend": None}, owner=person["guid"])


def event(type, by, data):
    """One entry of a conversation: 0.5's stream event without its seq (its position + 1)."""
    return json.dumps({"at": time.time(), "type": type, "by": by, "data": data}, ensure_ascii=False)


def wire(entry):
    """A raw stream entry as 0.5's event wire: {seq, at, type, by, data}."""
    got = json.loads(base64.b64decode(entry["data"]))
    return {"seq": entry["position"] + 1, "at": got.get("at"), "type": got.get("type"), "by": got.get("by"),
            "data": got.get("data")}


# ------------------------------------------------------------------------- a turn

def skill_text(path):
    """(front matter, body) of a skill file: `description:` and `when:` lines between --- lines."""
    text = pathlib.Path(path).read_text(encoding="utf-8")
    front = {}
    if text.startswith("---\n"):
        head, sep, body = text[4:].partition("\n---\n")
        if sep:
            for line in head.splitlines():
                key, colon, value = line.partition(":")
                if colon and key.strip():
                    front[key.strip()] = value.strip()
            return front, body
    return front, text


def system_prompt(call, record):
    """0.5's system prompt, as far as this slice has its parts: who it is, its persona, its skills."""
    body = record["body"]
    persona = body.get("prompt") or (HERE / "personas" / f"{body.get('persona') or HOUSE}.md").read_text("utf-8")
    parts = [f"You are {title(call, record)}, the agent \"{body['name']}\" on this Rook band "
             f"(identity agent:{body['name']}).", persona.strip()]
    have = call.skills()
    skills = []
    for name in body.get("skills") or ():
        if name in have:
            front, _ = skill_text(have[name][1])
            doc = " ".join(x for x in (front.get("description", ""),
                                       f"Use when {front['when']}" if front.get("when") else "") if x)
            skills.append(f"- {name}: {doc}")
    if skills:
        parts.append("## Skills\nRead a skill with agent.skill before you follow it.\n" + "\n".join(skills))
    parts += memory_parts(call, record)
    return "\n\n".join(p for p in parts if p)


def memory_parts(call, record):
    """The seam for the memory slice (Bake is designing memory): 0.5 added "## Notepad" (how to
    write it), "## Memory" and "## Notepad since your last dream" here. Nothing yet."""
    return []


async def carried(call):
    """The skills this turn carries in full (Plugin.carry), after 0.5's setup-brief rule."""
    out = ""
    for name, lead, path in await call.carried():
        _, body = skill_text(path)
        out += f"\n\n---\n{lead} The {name} skill, in full:\n\n{body.strip()}"
    return out


def engine_worker(call, backend):
    """The worker the engine runs on: the `worker` setting, else the one online worker announcing it."""
    named = call.settings["worker"]
    if named:
        return named
    serving = [w for w in call.workers() if w["online"] and backend in w["caps"]]
    if len(serving) == 1:
        return serving[0]["moniker"]
    if not serving:
        raise Error("no_engine", f"No online worker announces {backend}, the house agent's engine "
                                 f"(no single worker serves {backend}).",
                    f"Start a worker that serves {backend} (every Python worker gets it by default), or set the "
                    "engine with setting.set {plugin: agents, key: backend}.")
    raise Error("several_engines", f"{len(serving)} online workers announce {backend}: "
                                   f"{', '.join(w['moniker'] for w in serving)} (no single worker serves {backend}).",
                "Say which one the house agent thinks on: setting.set {plugin: agents, key: worker, value: <moniker>}.")


async def turn(call, record_guid, by, text):
    """One turn, as 0.5's Runtime.turn: the prompt, the engine, the reply. Never raises: a turn that
    fails says why in a status event, and the sender is answered with it."""
    record = call.get(record_guid)
    stream, prompt_written, token = record["body"]["stream"], False, None
    try:
        agent = house(call)
        backend = agent["body"].get("backend") or "default"
        if backend == "default":
            backend = call.settings["backend"] or ENGINE
        worker = engine_worker(call, backend)
        same = record["body"].get("harness_backend") == backend     # another engine can't resume this one's session
        args = {"prompt": f"Message from {by}:\n\n{text}{await carried(call)}", "system": system_prompt(call, agent)}
        model = (agent["body"].get("model") or {}).get("two")
        if model and model != "default":
            args["model"] = model
        for key, value in (("session", record["body"].get("harness") if same else None),
                           ("cwd", agent["body"].get("cwd"))):
            if value:
                args[key] = value
        name = title(call, agent)
        token = await call.call("hub", "token.create", {"purpose": "agent"},
                                f"The door token of {name}'s turn answering {by}; revoked when the turn ends.")
        _turning.add(token["token"])
        args["door"] = token["token"]
        moniker, pending = await call.start(worker, backend, args, f"agent {HOUSE} answers {by}")
        call.append(stream, event("turn", f"agent:{HOUSE}", {"role": "prompt", "text": text, "by": by,
                                                              **({"call": moniker} if moniker else {})}))
        prompt_written = True
        res = await pending
        if not isinstance(res, dict) or not isinstance(res.get("text"), str):
            raise Error("no_reply", "the engine gave no reply text", "Read the journal for the engine's call.")
        if res.get("session"):
            call.update(record_guid, body=dict(call.get(record_guid)["body"], harness=res["session"],
                                               harness_backend=backend))
        reply = {"role": "reply", "text": res["text"], **({"usage": res["usage"]} if res.get("usage") else {})}
        seq = call.append(stream, event("turn", f"agent:{HOUSE}", reply)) + 1
        return {"text": res["text"], "seq": seq}
    except Exception as e:                       # every failure is the sender's answer (0.5)
        message = e.message["message"] if isinstance(e, Error) else f"{type(e).__name__}: {e}"
        if not isinstance(e, Error):
            log.exception("The house agent's turn failed")
        if not prompt_written:
            call.append(stream, event("turn", f"agent:{HOUSE}", {"role": "prompt", "text": text, "by": by}))
        call.append(stream, event("status", f"agent:{HOUSE}", {"state": "error", "error": message}))
        return {"error": message}
    finally:
        if token is not None:
            _turning.discard(token["token"])
            try:
                await call.call("hub", "token.revoke", {"id": token["moniker"]}, "The turn ended: its door token goes.")
            except Error as e:
                log.warning("door token %s not revoked: %s", token["moniker"], e.message["message"])


# ------------------------------------------------------------------------------- caps

async def chat_send(call, args):
    record = _agent(call, args)
    text, wait = args.get("text"), args.get("wait", True)
    if not isinstance(text, str) or not text:
        raise Error("bad_message", "text is required: the message.", "Give the message as text.")
    # A message from a turn's own door token (the agent messaging itself) is taken like any other:
    # appended and queued, answered after the turn that sent it. It never waits, whatever `wait`
    # says, because its reply cannot come before that turn ends; and its own turn runs as the
    # person the token stood for, since the token is revoked when the sending turn ends.
    own = call._actor.get("token") in _turning
    person = call.person()
    by = f"person:{person['name']}"
    sess = session(call, person, create=True)
    seq = call.append(sess["body"]["stream"], event("input", by, {"text": text}), by_caller=True) + 1
    runner = call.as_caller(call.user) if own else call
    task = call.later(HOUSE, lambda: turn(runner, sess["guid"], by, text))
    out = {"agent": record["body"]["name"], "stream": _moniker(call, sess), "seq": seq}
    if not wait or own:
        return out
    got = await asyncio.shield(task)         # a sender that stops waiting leaves the turn to finish
    if "error" in got:
        raise Error("agent_failed", f"{record['body']['name']}: {got['error']}",
                    "Read the conversation with agent.chat.read, change what the error names, and send again.")
    return {**out, "text": got["text"], "reply_seq": got["seq"]}


def _moniker(call, sess):
    return call._hub.streams.get(sess["body"]["stream"])["moniker"]


async def chat_read(call, args):
    _agent(call, args)
    after, limit, wait, party = args.get("after", 0), args.get("limit"), args.get("wait", 0), args.get("party")
    person = call.person(party.removeprefix("person:")) if party else call.person()
    sess = session(call, person, create=False)
    if sess is None:
        return {"stream": None, "events": [], "next": 0, "closed": False}
    got = await call.read(sess["body"]["stream"], after, wait, limit)
    return {"stream": got["stream"], "events": [wire(e) for e in got["entries"]], "next": got["next"],
            "closed": not got["open"]}


def skill(call, args):
    name = args.get("name")
    have = call.skills()
    if name not in have:
        raise Error("unknown_skill", f"No skill {name}.",
                    f"The skills are {', '.join(sorted(have)) or 'none yet'}.")
    front, body = skill_text(have[name][1])
    return {"name": name, **front, "text": body.strip()}


def agent_list(call, args):
    return {"agents": [{"name": r["body"]["name"], "title": title(call, r), "summary": r["description"],
                        "persona": r["body"].get("persona"), "backend": r["body"].get("backend"),
                        "moniker": r["moniker"]} for r in call.search(AGENT, limit=-1)]}


def ready(call):
    """Whether a turn of the house agent can run now: its engine's worker is online and announces it."""
    backend = house(call)["body"].get("backend") or "default"
    if backend == "default":
        backend = call.settings["backend"] or ENGINE
    try:
        named = engine_worker(call, backend)
    except Error:
        return False
    return any(named in (w["moniker"], w["name"], w["guid"]) and w["online"] and backend in w["caps"]
               for w in call.workers())


def _from_parts(name):
    return next((getattr(m, name) for m in _parts if callable(getattr(m, name, None))), None)


async def site(call, args):
    """What the hub site's chat dock needs (0.5's agent.site): {dock, ready, look, setup}."""
    look, setup_state = _from_parts("look_state"), _from_parts("setup_state")
    look = look(call) if look else {"sound": False, "rookie": "pixel", "theme": "stone",
                                    "name": call.settings["house_name"] or HOUSE_NAME,
                                    "chosen": {"sound": True, "rookie": True, "theme": True, "name": True},
                                    "next": None, "styles": [], "themes": []}
    setup_state = setup_state(call) if setup_state else {"done": True}
    if asyncio.iscoroutine(look):
        look = await look
    if asyncio.iscoroutine(setup_state):
        setup_state = await setup_state
    return {"dock": call.settings["dock"] or None, "ready": ready(call), "look": look, "setup": setup_state}


def made(call):
    """At every load, as the system owner: the house agent exists; a message left with no answer
    when the hub stopped is answered, as 0.5 answered it, so its sender knows to send it again."""
    if not any(r["body"].get("name") == HOUSE for r in call.search(AGENT, limit=-1)):
        call.create(AGENT, PERSONA["title"], PERSONA["summary"], body={k: v for k, v in PERSONA.items()
                                                                        if k not in ("title", "summary")},
                    owner=call.user)
    if call.busy(HOUSE):
        return                                   # a turn is running now (a replace while serving)
    for sess in _sessions(call):
        stream = call._hub.streams
        with call._hub._lock:
            last = call._hub._db.execute("SELECT data FROM stream_data WHERE stream = ? ORDER BY position DESC "
                                         "LIMIT 1", (sess["body"]["stream"],)).fetchone()
        if last is None:
            continue
        got = json.loads(last["data"])
        if got.get("type") == "input" or (got.get("type") == "turn" and got["data"].get("role") == "prompt"):
            if stream.get(sess["body"]["stream"])["open"]:
                call.append(sess["body"]["stream"], event("status", f"agent:{HOUSE}", {
                    "state": "error", "error": "nobody is waiting on this message any more (the hub restarted "
                                               "before the agent answered); send it again"}))


_TEXT = {"type": "string"}
CAPS = {
    "agent.chat.send": (chat_send, "Message an agent in your conversation with it; with wait, answers its reply.", {
        "type": "object", "required": ["agent", "text"], "additionalProperties": False, "properties": {
            "agent": {"type": "string", "description": "The agent: house."},
            "text": {"type": "string", "description": "The message."},
            "wait": {"type": "boolean", "description": "Wait for the reply (default); false: answer at once, "
                                                       "read it later with agent.chat.read."}}}),
    "agent.chat.read": (chat_read, "Your conversation with an agent, after a cursor (party: someone else's).", {
        "type": "object", "required": ["agent"], "additionalProperties": False, "properties": {
            "agent": {"type": "string", "description": "The agent: house."},
            "after": {"type": "integer", "minimum": 0, "description": "The last seq you saw; pass back next."},
            "limit": {"type": "integer", "minimum": 1, "description": "At most this many events; none: all."},
            "wait": {"type": "number", "minimum": 0, "description": "Seconds to wait for something new."},
            "party": {"type": "string", "description": "Whose conversation: person:<name>, or a user's name, "
                                                       "moniker or GUID; none: yours."}}}),
    "agent.skill": (skill, "A skill's playbook (markdown) by name, to read before following it.", {
        "type": "object", "required": ["name"], "additionalProperties": False, "properties": {
            "name": {"type": "string", "description": "The skill, as an agent's prompt lists it."}}}),
    "agent.list": (agent_list, "The agents on this hub: the house agent, its name and engine.", {
        "type": "object", "additionalProperties": False, "properties": {}}),
    "agent.site": (site, "What the hub site's chat dock needs: the agent in it, whether it can answer yet, "
                         "your look and feel, and whether first-run setup is done.", {
        "type": "object", "additionalProperties": False, "properties": {}}),
}
