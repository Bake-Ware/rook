"""A stand-in for Chaquopy's ``java`` module, for the phone plugin ports
(rook/plugins/*-android and the other ``platforms: [android]`` plugins).

``jclass(name)`` hands out one MagicMock per class name, so a test sets what
it needs (``j.cls("systems.bake.rook.SpeakBridge").stop.return_value = ...``)
and asserts on the calls. The app context, permissions and content provider
queries are real fakes: a context whose ``checkSelfPermission`` answers from
``j.granted`` and whose resolver answers ``query`` from ``j.tables``.
"""

import json
import sys
import types
from unittest.mock import MagicMock

import pytest

GRANTED, DENIED = 0, -1


class Cursor:
    """android.database.Cursor over a list of row dicts."""

    def __init__(self, rows, columns=None):
        self.columns = list(columns or (rows[0].keys() if rows else []))
        self.rows, self.i, self.closed = rows, -1, False

    def getColumnNames(self):
        return self.columns

    def moveToNext(self):
        self.i += 1
        return self.i < len(self.rows)

    def _v(self, i):
        return self.rows[self.i].get(self.columns[i])

    def getType(self, i):
        v = self._v(i)
        return 0 if v is None else 1 if isinstance(v, (bool, int)) else 2 if isinstance(v, float) else 3

    def getLong(self, i):
        return int(self._v(i))

    def getDouble(self, i):
        return float(self._v(i))

    def getString(self, i):
        return str(self._v(i))

    def close(self):
        self.closed = True


class FakeJava:
    def __init__(self):
        self.classes: dict[str, MagicMock] = {}
        self.granted: set[str] = set()
        self.tables: dict[str, list[dict]] = {}       # uri -> rows (filtered by the test's own where)
        self.queries: list[tuple] = []
        self.prefs: dict[str, object] = {}
        self.started: list = []                       # intents passed to startActivity
        self.ctx = MagicMock(name="Context")
        self.ctx.checkSelfPermission.side_effect = lambda p: GRANTED if p in self.granted else DENIED
        self.ctx.getContentResolver.return_value.query.side_effect = self._query
        self.ctx.startActivity.side_effect = self.started.append
        prefs = self.ctx.getSharedPreferences.return_value
        prefs.getBoolean.side_effect = lambda k, d: self.prefs.get(k, d)
        self.cls("android.app.ActivityThread").currentApplication.return_value = self.ctx
        self.cls("android.content.pm.PackageManager").PERMISSION_GRANTED = GRANTED
        self.cls("android.net.Uri").parse.side_effect = lambda s: s
        self.cls("android.os.Build$VERSION").SDK_INT = 34
        self.cls("android.provider.Settings").canDrawOverlays.return_value = True
        # Every getSystemService(name) is its own mock, found again by name.
        self.services: dict[str, MagicMock] = {}
        Context = self.cls("android.content.Context")
        for name in ("AUDIO_SERVICE", "NOTIFICATION_SERVICE", "BATTERY_SERVICE", "CLIPBOARD_SERVICE",
                     "CAMERA_SERVICE", "VIBRATOR_SERVICE", "VIBRATOR_MANAGER_SERVICE", "WINDOW_SERVICE",
                     "KEYGUARD_SERVICE", "MEDIA_SESSION_SERVICE"):
            setattr(Context, name, name)
        self.ctx.getSystemService.side_effect = lambda n: self.services.setdefault(n, MagicMock(name=n))
        self.service("KEYGUARD_SERVICE").isKeyguardLocked.return_value = False
        # Intents keep what was put in them.
        self.cls("android.content.Intent").side_effect = lambda *a: Intent(*a)
        keep = self.cls("systems.bake.rook.KeepAwake")
        keep.activity.return_value = json.dumps({"ok": True, "auto": False})

    def cls(self, name: str) -> MagicMock:
        return self.classes.setdefault(name, MagicMock(name=name))

    def app(self, name: str) -> MagicMock:
        return self.cls("systems.bake.rook." + name)

    def service(self, name: str) -> MagicMock:
        return self.services.setdefault(name, MagicMock(name=name))

    def _query(self, uri, columns, where, args, order):
        self.queries.append((uri, columns, where, args, order))
        rows = self.tables.get(uri)
        if rows is None:
            return None
        return Cursor([{c: r.get(c) for c in columns} for r in rows] if columns else rows, columns)

    def module(self) -> types.ModuleType:
        m = types.ModuleType("java")
        m.jclass = self.cls
        m.cast = lambda cls, obj: obj
        m.jint = lambda v: ("jint", v)
        return m


class Intent:
    """android.content.Intent, as data."""

    def __init__(self, action=None, data=None):
        self.action, self.data, self.mime = action, data, None
        self.extras, self.flags, self.categories, self.component = {}, [], [], None

    def setData(self, uri):
        self.data = uri

    def setType(self, mime):
        self.mime = mime

    def setDataAndType(self, uri, mime):
        self.data, self.mime = uri, mime

    def putExtra(self, k, v):
        self.extras[k] = v

    def addFlags(self, f):
        self.flags.append(f)

    def addCategory(self, c):
        self.categories.append(c)

    def setComponent(self, c):
        self.component = c


@pytest.fixture
def java(monkeypatch):
    j = FakeJava()
    monkeypatch.setitem(sys.modules, "java", j.module())
    # Screen waits poll; nothing here needs real time.
    import rook.kit.android.screen as screen
    monkeypatch.setattr(screen, "POLL_S", 0.001)
    monkeypatch.setattr(screen.time, "sleep", lambda s: None)
    return j
