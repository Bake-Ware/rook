"""clipboard-android: the clipboard of the phone the Rook Android app runs on (brief AP2), shipped as a
plugin installed by default. clipboard.get and clipboard.set are 0.5's (origin/castling
rook/plugins/clipboard-android): the same names, arguments and replies.

Android 10 and later show the clipboard only to the app in front. So clipboard.get reads it from a
moment in front: the app's bridge (systems.bake.rook.core.worker.Bridge, in the app) shows a see-through
screen of its own for as long as the read takes, and Android lets an app in the background do that
only once the person has let it display over other apps. The first call asks for that, and answers
once the person has; refused, the error says how to allow it. 0.5 read it from the background, and
got nothing on Android 10 and later.

Off the app it announces nothing: CAPS is empty wherever Chaquopy's `java` module does not import,
which is everywhere but the app."""

import asyncio
import json

from rook_worker import Cap

try:
    from java import cast, jclass     # Chaquopy's: only in the Rook Android app
except ImportError:
    jclass = None


def _check(args, cap, allowed, required=()):
    """Refuse an argument the cap does not take, so it is never silently ignored (C3)."""
    extra = sorted(set(args) - set(allowed))
    if extra:
        raise ValueError(f"{cap} takes no argument {', '.join(extra)}; it takes {', '.join(allowed) or 'none'}")
    for name in required:
        if args.get(name) is None:
            raise ValueError(f"{cap} needs {name}")


async def get(args):
    _check(args, "clipboard.get", ())
    bridge = jclass("systems.bake.rook.core.worker.Bridge")
    read = json.loads(str(await asyncio.to_thread(bridge.clipboard, "clipboard.get")))
    if "error" in read:
        raise ValueError(read["error"])
    if read["text"] is None:
        return {"text": None, "note": "the clipboard is empty"}
    return {"text": read["text"]}


def _set(text):
    context = jclass("systems.bake.rook.core.worker.Bridge").context()
    Context = jclass("android.content.Context")
    manager = cast(jclass("android.content.ClipboardManager"), context.getSystemService(Context.CLIPBOARD_SERVICE))
    manager.setPrimaryClip(jclass("android.content.ClipData").newPlainText("rook", text))
    return {"chars": len(text)}


async def set_text(args):
    _check(args, "clipboard.set", ("text",), ("text",))
    if not isinstance(args["text"], str):
        raise ValueError("text must be a string")
    return await asyncio.to_thread(_set, args["text"])


CAPS = {} if jclass is None else {
    "clipboard.get": Cap(
        "The phone's clipboard text: {text}; text null, with a note, when the clipboard is empty.",
        {"type": "object", "additionalProperties": False, "properties": {}},
        get),
    "clipboard.set": Cap(
        "Put text on the phone's clipboard: {chars}.",
        {"type": "object", "additionalProperties": False, "required": ["text"],
         "properties": {"text": {"type": "string"}}},
        set_text),
}
