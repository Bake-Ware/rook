"""clipboard.*: ClipboardManager."""

from rook.kit import android


def _cm():
    return android.service("CLIPBOARD_SERVICE", "android.content.ClipboardManager")


def get(ctx):
    clip = _cm().getPrimaryClip()
    if clip is None or clip.getItemCount() == 0:
        return {"text": None, "note": "empty, or hidden from apps in the background (Android 10+)"}
    text = clip.getItemAt(0).coerceToText(android.context())
    return {"text": str(text) if text is not None else None}


def set_text(ctx, text):
    _cm().setPrimaryClip(android.jclass("android.content.ClipData").newPlainText("rook", text))
    return {"chars": len(text)}
