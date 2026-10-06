"""notify.* — read/dismiss/post notifications (Chaquopy java bridge).

Reading needs the "Notification access" special grant (enabled in the app);
posting only needs POST_NOTIFICATIONS. Backed by RookNotificationListener, which
buffers every posted notification — so notify.list catches message previews from
every app (SMS, WhatsApp, Signal, email…), not just SMS.
"""

from __future__ import annotations

import json
import time

from rook.worker.plugin import Plugin, capability
from rook_android.androidctx import app_context, jclass

try:
    from java import jclass as _jclass
    _Listener = _jclass("systems.bake.rook.RookNotificationListener")
except Exception:  # pragma: no cover
    _Listener = None


def _packages(value) -> set[str]:
    """Package filter from a list or a comma/space separated string (empty = all)."""
    if value is None:
        return set()
    parts = value if isinstance(value, (list, tuple, set)) else str(value).replace(",", " ").split()
    return {str(p).strip() for p in parts if str(p).strip()}


class AndroidNotifyPlugin(Plugin):
    NAMESPACE = "notify"

    @capability("list")
    def _list(self, limit: int = 40, packages: str | list | None = None) -> dict:
        """Recent notifications, newest first.

        Args:
          limit: most notifications to return (1-200, default 40).
          packages: only these apps, as a list or comma-separated package names,
            e.g. "com.google.android.gm,com.microsoft.office.outlook" for mail.

        Each item: {package, title, text, ts (epoch s), posted_ms (epoch ms), key,
        clearable, sub_text?, big_text?}. Mail apps usually put the sender in
        title, the subject in text, the account in sub_text and a preview in big_text.
        """
        if _Listener is None:
            return {"ok": False, "error": "not an Android host"}
        ctx = app_context()
        if ctx is not None and not _Listener.isEnabled(ctx):
            return {"ok": False, "error": "notification access not granted (grant it in the app)"}
        try:
            n = max(1, min(int(limit), 200))
            wanted = _packages(packages)
            raw = _Listener.snapshotJson(200 if wanted else n)
            items = json.loads(str(raw))
            if wanted:
                items = [i for i in items if i.get("package") in wanted][:n]
            for i in items:
                if "posted_ms" not in i and isinstance(i.get("ts"), (int, float)):
                    i["posted_ms"] = int(i["ts"] * 1000)
            return {"ok": True, "count": len(items), "connected": bool(_Listener.isConnected()),
                    "notifications": items}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    @capability("dismiss")
    def _dismiss(self, key: str) -> dict:
        """Dismiss a notification by its ``key`` (from notify.list)."""
        if _Listener is None:
            return {"ok": False, "error": "not an Android host"}
        try:
            ok = bool(_Listener.dismiss(str(key)))
            return {"ok": ok} if ok else {"ok": False, "error": "not dismissed (listener not connected?)"}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    @capability("post")
    def _post(self, title: str = "rook", text: str = "") -> dict:
        """Post a local notification on the phone (needs POST_NOTIFICATIONS)."""
        ctx = app_context()
        if ctx is None:
            return {"ok": False, "error": "not an Android host"}
        try:
            Context = jclass("android.content.Context")
            NotificationManager = jclass("android.app.NotificationManager")
            NotificationChannel = jclass("android.app.NotificationChannel")
            Notification = jclass("android.app.Notification")
            Builder = jclass("android.app.Notification$Builder")
            VER = jclass("android.os.Build$VERSION")
            nm = ctx.getSystemService(Context.NOTIFICATION_SERVICE)
            chan_id = "rook_msgs"
            if VER.SDK_INT >= 26:
                ch = NotificationChannel(chan_id, "Rook messages", NotificationManager.IMPORTANCE_DEFAULT)
                nm.createNotificationChannel(ch)
                b = Builder(ctx, chan_id)
            else:
                b = Builder(ctx)
            icon = jclass("android.R$drawable").stat_notify_chat
            b.setContentTitle(str(title)).setContentText(str(text)).setSmallIcon(icon).setAutoCancel(True)
            b.setContentIntent(jclass("systems.bake.rook.NotificationNavigation").mainActivity(ctx))
            nid = int(time.time()) & 0x7fffffff
            nm.notify(nid, b.build())
            return {"ok": True, "id": nid}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}


PLUGIN = AndroidNotifyPlugin
