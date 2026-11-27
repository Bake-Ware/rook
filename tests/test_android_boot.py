"""After a reboot, before anyone unlocks the phone (brief AP8). The worker links then and the caps run, but
Android keeps some things closed until the first unlock: the calendar, contacts, call log and texts, the
phone's shared storage (/sdcard), every app that does not run before then, and Rook's own screens (its
asks and the clipboard read; Bridge.kt says so itself). Each cap that needs one of them says plainly that
the phone has not been unlocked since it restarted and that phone.unlock does that, rather than "the
phone gave no answer", "not found" or a Java stack. Proved on the emulator in the pull request; here,
against a stand-in for Chaquopy's `java` module whose Bridge says the phone is locked or not. The move of
an older install's state into storage read before the unlock is tested on the JVM (StorageTest.kt)."""

import asyncio
import types

import pytest

from test_android_plugins import part

LOCKED = ("this phone has not been unlocked since it restarted, and until it is, Android {what}. "
          "phone.unlock unlocks it (pin: secret:<name>, or the worker's unlock_pin); then call again.")


class Android:
    """What the plugins reach through `jclass`, for these tests: Bridge (locked or not), a content resolver
    that answers None or raises as Android's do before the first unlock, and a package manager."""

    MATCH_DIRECT_BOOT_AWARE, MATCH_DIRECT_BOOT_UNAWARE = 0x40000, 0x80000

    def __init__(self, locked, query=None, apps=(), launchable=()):
        self.is_locked, self.answer, self.apps, self.launchable = locked, query, list(apps), set(launchable)
        self.asked_flags = []
        self.said = []

    # Bridge
    def locked(self, what):
        self.said.append(what)
        return LOCKED.format(what=what) if self.is_locked else None

    def ask(self, permission, cap):
        return None

    def context(self):
        return self

    def getContentResolver(self):
        return self

    def query(self, *args):
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    # PackageManager, Intent, Uri
    def getPackageManager(self):
        return self

    def queryIntentActivities(self, intent, flags):
        self.asked_flags.append(flags)
        out = [types.SimpleNamespace(activityInfo=types.SimpleNamespace(
            packageName=package, loadLabel=lambda _pm, label=label: label)) for label, package in self.apps]
        return types.SimpleNamespace(size=lambda: len(out), get=lambda k: out[k])

    def getLaunchIntentForPackage(self, package):
        return object() if package in self.launchable else None

    def parse(self, uri):
        return uri

    def java(self):
        java = types.ModuleType("java")
        names = {"systems.bake.rook.core.worker.Bridge": self, "android.net.Uri": self,
                 "android.content.pm.PackageManager": self,
                 "android.content.Intent": type("Intent", (), {"ACTION_MAIN": "main", "CATEGORY_LAUNCHER": "launcher",
                                                                "__init__": lambda s, a: None,
                                                                "addCategory": lambda s, c: None})}
        java.jclass = lambda name: names[name] if name in names else pytest.fail(f"reached Android for {name}")
        java.cast = lambda kind, value: value
        java.jint = int
        java.dynamic_proxy = lambda *a: object
        return java


PROVIDERS = {"calendar": "the calendar", "contact": "the contacts", "call": "the call log", "sms": "the texts"}


@pytest.mark.parametrize("name", sorted(PROVIDERS))
def test_before_the_first_unlock_a_provider_that_answers_nothing_says_why(name):
    android = Android(locked=True, query=None)
    with pytest.raises(ValueError) as said:
        part(name, android.java())["_query"]("content://x", ["a"])
    assert str(said.value) == LOCKED.format(what=f"keeps {PROVIDERS[name]} closed")


@pytest.mark.parametrize("name", sorted(PROVIDERS))
def test_before_the_first_unlock_a_provider_that_throws_says_why(name):
    # The texts' provider throws (its database is in storage Android has not opened) rather than answering None.
    android = Android(locked=True, query=RuntimeError("Cannot open database '/data/user/0/...mmssms.db'"))
    with pytest.raises(ValueError) as said:
        part(name, android.java())["_query"]("content://x", ["a"])
    assert "has not been unlocked since it restarted" in str(said.value) and "mmssms" not in str(said.value)


@pytest.mark.parametrize("name", sorted(PROVIDERS))
def test_once_unlocked_a_provider_says_what_it_said_before(name):
    with pytest.raises(ValueError) as said:
        part(name, Android(locked=False, query=None).java())["_query"]("content://x", ["a"])
    assert str(said.value) == "content://x: the phone gave no answer"
    with pytest.raises(RuntimeError):
        part(name, Android(locked=False, query=RuntimeError("other")).java())["_query"]("content://x", ["a"])


def test_app_list_lists_every_app_before_the_first_unlock_too():
    android = Android(locked=True, apps=[("Chrome", "com.android.chrome"), ("Clock", "com.google.android.deskclock")])
    apps = part("app-android", android.java())["launcher_apps"]()
    assert [a["label"] for a in apps] == ["Chrome", "Clock"]
    assert android.asked_flags == [Android.MATCH_DIRECT_BOOT_AWARE | Android.MATCH_DIRECT_BOOT_UNAWARE]


def test_app_launch_of_an_app_that_does_not_run_before_the_first_unlock_says_why():
    android = Android(locked=True, apps=[("Chrome", "com.android.chrome")])
    with pytest.raises(ValueError) as said:
        part("app-android", android.java())["_launch"]("Chrome", {}, False)
    assert str(said.value) == LOCKED.format(what="opens only the apps that run before then (such as the clock, the phone "
                                                 "and settings), and Chrome is not one")
    with pytest.raises(ValueError) as said:
        part("app-android", Android(locked=False, apps=[("Chrome", "com.android.chrome")]).java())["_launch"]("Chrome", {}, False)
    assert str(said.value) == "com.android.chrome can't be launched"


def run(handler, args):
    return asyncio.run(handler(args))


def test_file_caps_on_shared_storage_before_the_first_unlock_say_why():
    caps = part("file", Android(locked=True).java())["CAPS"]
    closed = LOCKED.format(what="keeps the phone's shared storage (/sdcard) closed")
    for cap, args in (("file.list", {"path": "/sdcard/nothing-here"}), ("file.read", {"path": "/sdcard/Download/x.txt"}),
                      ("file.write", {"path": "/sdcard/Download/x.txt", "content": "x"}),
                      ("file.search", {"pattern": "x", "path": "/sdcard/x"})):
        with pytest.raises(ValueError) as said:
            run(caps[cap].handler, args)
        assert str(said.value).endswith(closed), cap
        assert str(said.value).startswith("not found: "), cap
    # file.stat answers as it always does for a path that is not there, and says why it may not be.
    assert run(caps["file.stat"].handler, {"path": "/storage/emulated/0/x"}) == {"exists": False, "note": closed}


def test_file_caps_elsewhere_or_once_unlocked_or_off_the_app_say_what_they_said_before(tmp_path):
    missing = str(tmp_path / "missing")
    locked = part("file", Android(locked=True).java())["CAPS"]
    with pytest.raises(ValueError) as said:
        run(locked["file.list"].handler, {"path": missing})        # not shared storage: not the lock's doing
    assert str(said.value) == f"not found: {missing}; give a folder's path; describe a file with file.stat"
    unlocked = part("file", Android(locked=False).java())["CAPS"]
    with pytest.raises(ValueError) as said:
        run(unlocked["file.list"].handler, {"path": "/sdcard/nothing-here"})
    assert str(said.value) == "not found: /sdcard/nothing-here; give a folder's path; describe a file with file.stat"
    off_the_app = part("file")["CAPS"]
    with pytest.raises(ValueError) as said:
        run(off_the_app["file.read"].handler, {"path": "/sdcard/nothing-here"})
    assert "unlocked" not in str(said.value)
    assert run(off_the_app["file.stat"].handler, {"path": "/sdcard/nothing-here"}) == {"exists": False}
