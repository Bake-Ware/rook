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


class AndroidNotifyPlugin(Plugin):
    NAMESPACE = "notify"

    @capability("list")
    def _list(self, limit: int = 40) -> dict:
        """Recent notifications, newest first: {package, title, text, ts, key}."""
        if _Listener is None:
            return {"ok": False, "error": "not an Android host"}
        ctx = app_context()
        if ctx is not None and not _Listener.isEnabled(ctx):
            return {"ok": False, "error": "notification access not granted (grant it in the app)"}
        try:
            raw = _Listener.snapshotJson(max(1, min(int(limit), 200)))
            items = json.loads(str(raw))
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
            nid = int(time.time()) & 0x7fffffff
            nm.notify(nid, b.build())
            return {"ok": True, "id": nid}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}


PLUGIN = AndroidNotifyPlugin
