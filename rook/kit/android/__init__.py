"""Java for the phone plugins.

The Rook Android app runs the 0.5 worker under Chaquopy (rook.core2.worker.android);
its plugins (rook/plugins/*, ``platforms: [android]``) reach Android through
Chaquopy's ``java`` module and the app's own Kotlin bridges
(``systems.bake.rook.*``). Plugin code is plain ``def cap(ctx, **args)``: the
host runs it in a thread, so blocking Java calls are fine.

    from rook.kit import android
    android.need("READ_SMS")                                  # or KitError naming the grant
    rows = android.query("content://sms/inbox", ["address", "body"], order="date DESC", limit=30)
    out = android.result(android.app("SpeakBridge").stop())   # a bridge's JSON reply, errors raised
    android.start(intent)                                     # an activity, from the background

Every failure is a KitError a person can act on. Off the phone ``java``
doesn't import and every helper says "not an Android host"; tests put a fake
``java`` module in ``sys.modules``.
"""

from __future__ import annotations

import json
from typing import Any

from rook.kit.ctx import KitError

APP = "systems.bake.rook."
NOT_HERE = "not an Android host"
NOT_ALLOWED = ("this phone doesn't let the assistant act for its owner: on the phone, Settings > App > "
               "\"Let the assistant act for me on this phone\"")


def _java():
    try:
        import java   # Chaquopy
    except ImportError:
        raise KitError(NOT_HERE) from None
    return java


def jclass(name: str) -> Any:
    return _java().jclass(name)


def cast(class_name: str, obj: Any) -> Any:
    return _java().cast(jclass(class_name), obj)


def jint(value: int) -> Any:
    """A Java int (an extra read with getIntExtra must not arrive as a long)."""
    return _java().jint(int(value))


def app(name: str) -> Any:
    """One of the app's Kotlin bridge classes, ``systems.bake.rook.<name>``."""
    return jclass(APP + name)


def context() -> Any:
    """The app's Application context, fetched without Kotlin handing it over."""
    try:
        ctx = jclass("android.app.ActivityThread").currentApplication()
    except KitError:
        raise
    except Exception:
        ctx = None
    if ctx is None:
        raise KitError(NOT_HERE)
    return ctx


def granted(permission: str) -> bool:
    """A runtime permission (``READ_SMS`` or its full name) is granted to the app."""
    name = permission if "." in permission else "android.permission." + permission
    return context().checkSelfPermission(name) == jclass("android.content.pm.PackageManager").PERMISSION_GRANTED


def need(*permissions: str) -> None:
    missing = [p for p in permissions if not granted(p)]
    if missing:
        raise KitError(f"{', '.join(missing)} not granted: allow it in the Rook app (Settings > Permissions)")


def service(name: str, class_name: str) -> Any:
    """A system service: ``service("AUDIO_SERVICE", "android.media.AudioManager")``."""
    Context = jclass("android.content.Context")
    return cast(class_name, context().getSystemService(getattr(Context, name)))


def assistant_may_act() -> None:
    """The phone owner's own switch for acting as them (calendar changes,
    a notification's reply). Off by default; raises when off."""
    try:
        on = bool(context().getSharedPreferences("rook", 0).getBoolean("assistant_actions", False))
    except KitError:
        raise
    except Exception:
        on = False
    if not on:
        raise KitError(NOT_ALLOWED)


def result(raw: Any) -> Any:
    """A bridge's JSON reply, ``ok`` dropped. ``{"ok": false, "error": ...}``
    raises, with what else it said (near matches, the fields on screen)."""
    if raw is None:
        raise KitError("the app gave no answer")
    out = json.loads(str(raw))
    if isinstance(out, dict) and out.pop("ok", True) is False:
        error = str(out.pop("error", None) or "the app refused")
        raise KitError(f"{error} {json.dumps(out, ensure_ascii=False)}" if out else error)
    return out


def query(uri: str, columns: list[str] | None = None, where: str | None = None,
          args: list[str] | None = None, order: str | None = None, limit: int | None = None) -> list[dict]:
    """Rows from a content provider, each column as str, int, float or None
    (blobs are left out). ``limit`` stops reading after that many rows."""
    cur = context().getContentResolver().query(jclass("android.net.Uri").parse(uri), columns, where, args, order)
    if cur is None:
        raise KitError(f"{uri}: the provider gave no cursor")
    rows: list[dict] = []
    try:
        names = [str(n) for n in cur.getColumnNames()]
        while (limit is None or len(rows) < limit) and cur.moveToNext():
            rows.append({n: _value(cur, i) for i, n in enumerate(names)})
    finally:
        cur.close()
    return rows


def _value(cur: Any, i: int) -> Any:
    kind = int(cur.getType(i))   # Cursor.FIELD_TYPE_NULL, INTEGER, FLOAT, STRING, BLOB
    if kind == 1:
        return int(cur.getLong(i))
    if kind == 2:
        return float(cur.getDouble(i))
    if kind == 3:
        return str(cur.getString(i))
    return None


def cursor(ctx: Any, name: str, value: int | None = None) -> int | None:
    """Where a watcher got to, kept in the plugin's data folder so a restart
    picks up what arrived meanwhile: read it (None the first time), or save ``value``."""
    path = ctx.data / f"{name}.cursor"
    if value is not None:
        path.write_text(str(int(value)), encoding="ascii")
        return value
    try:
        return int(path.read_text(encoding="ascii"))
    except (OSError, ValueError):
        return None


def intent(action: str, data: str | None = None, mime: str | None = None,
           extras: dict | None = None) -> Any:
    """An Intent. Extras: bool and str as they are, numbers as Java ints."""
    Intent = jclass("android.content.Intent")
    i = Intent(action)
    uri = jclass("android.net.Uri").parse(data) if data else None
    if uri is not None and mime:
        i.setDataAndType(uri, mime)
    elif uri is not None:
        i.setData(uri)
    elif mime:
        i.setType(mime)
    for name, value in (extras or {}).items():
        i.putExtra(name, value if isinstance(value, (bool, str)) else jint(value))
    return i


def handler(i: Any) -> str | None:
    """The package that would take an intent (None when Android asks or hides it)."""
    try:
        info = context().getPackageManager().resolveActivity(i, 0)
        pkg = str(info.activityInfo.packageName) if info is not None else None
    except Exception:
        return None
    return pkg if pkg and pkg != "android" else None


def start(i: Any) -> dict:
    """Start an activity from the background. Returns ``{}``, or a note when
    Android may have refused to show it (10+, without an exemption)."""
    Intent = jclass("android.content.Intent")
    i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    try:
        context().startActivity(i)
    except Exception as e:
        if "ActivityNotFound" in type(e).__name__ or "No Activity" in str(e):
            raise KitError("no app on this phone handles that") from None
        raise
    return background_note()


def background_note() -> dict:
    """Android 10+ silently refuses most background activity starts. Rook is
    exempt while it may draw over other apps or its accessibility service is on."""
    try:
        if int(jclass("android.os.Build$VERSION").SDK_INT) < 29 \
                or jclass("android.provider.Settings").canDrawOverlays(context()) \
                or app("HidAccessibilityService").isEnabled():
            return {}
    except Exception:
        return {}
    return {"may_be_blocked": True,
            "note": "Android may not show this: allow 'Display over other apps' or the accessibility "
                    "service in the Rook app; ui.screen shows what opened"}
