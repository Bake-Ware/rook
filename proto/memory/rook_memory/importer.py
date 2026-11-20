"""Taking conversations in: a Claude data export, or the made-up test set (the same shape).

The export is a zip (or its unpacked folder, or the file itself) whose `conversations.json` is a list
of conversations, each with a name, timestamps and `chat_messages` marked "human" or "assistant".
Bake's words and the assistant's final answers are kept; tool calls, tool results and thinking are
dropped. The human there is Bake talking directly; the assistant is an agent. Every entry is stamped
with who said it on the way in (in the real thing the hub does that).

The made-up set adds, per message, "speaker" (bake, steward, agent, other, or house for the house
agent's own lines, which `play` does not send) and "channel" (chat or voice), so it can exercise who-said-it.
"""

import json
import pathlib
import zipfile


def _load(path):
    p = pathlib.Path(path).expanduser()
    if p.is_dir():
        p = p / "conversations.json"
    if p.suffix == ".zip":
        with zipfile.ZipFile(p) as z:
            name = next(n for n in z.namelist() if n.endswith("conversations.json"))
            return json.loads(z.read(name))
    return json.loads(p.read_text(encoding="utf-8"))


def _text(msg, role, knobs):
    content = msg.get("content")
    if not isinstance(content, list) or not content:
        return str(msg.get("text") or "")
    tools = [i for i, b in enumerate(content) if b.get("type") in ("tool_use", "tool_result")]
    blocks = content
    if role == "assistant" and tools and knobs["import"]["assistant_text"] == "final":
        blocks = content[tools[-1] + 1:]
    text = "\n".join(str(b.get("text") or "") for b in blocks if b.get("type") == "text").strip()
    return text if text or tools else str(msg.get("text") or "")


def conversations(path, knobs):
    """Answers the conversations, oldest first, each with its messages stamped."""
    k = knobs["import"]
    out = []
    for c in _load(path):
        msgs = []
        for m in c.get("chat_messages") or c.get("messages") or []:
            role = "assistant" if m.get("sender") in ("assistant", "agent") else "human"
            speaker = m.get("speaker") or (k["export_assistant"] if role == "assistant" else k["export_human"])
            text = _text(m, role, knobs)
            if role == "human" and k["note_attachments"]:
                names = [a.get("file_name") for a in (m.get("attachments") or []) + (m.get("files") or []) if a.get("file_name")]
                if names:
                    text = (text + "\n" if text else "") + "[attached: " + ", ".join(names) + "]"
            if not text.strip():
                continue
            msgs.append({"at": m.get("created_at") or c.get("created_at"), "speaker": speaker,
                         "house": speaker == "house" or (role == "assistant" and not m.get("speaker")),
                         "channel": m.get("channel") or "chat", "text": text.strip()})
        out.append({"id": c.get("uuid") or c.get("id"), "name": c.get("name") or "(no name)",
                    "created_at": c.get("created_at") or (msgs[0]["at"] if msgs else ""),
                    "made_up": bool(c.get("made_up")), "messages": msgs})
    return sorted(out, key=lambda c: str(c["created_at"]))


def pick(convs, picks=(), since=None, until=None):
    out = []
    for c in convs:
        if picks and not any(p.lower() in c["name"].lower() or p == c["id"] for p in picks):
            continue
        if since and str(c["created_at"]) < since:
            continue
        if until and str(c["created_at"]) > until:
            continue
        out.append(c)
    return out


def take(store, knobs, path, convs, log=print):
    """Records each conversation's messages (the ones not taken before) as a session. Answers the session ids."""
    source = str(pathlib.Path(path).expanduser().resolve())
    made = []
    for c in convs:
        s = store.session(c["name"], source, c["id"], c["made_up"])
        have = store.one("SELECT COUNT(*) AS n FROM entries WHERE session = ?", s["id"])["n"]
        new = c["messages"][have:]
        for m in new:
            store.add_entry(s["id"], m["at"], m["speaker"], m["channel"], m["text"])
        log(f"{c['name']}: {len(new)} new entries" + (f" ({have} taken before)" if have else ""))
        made.append(s["id"])
    return made
