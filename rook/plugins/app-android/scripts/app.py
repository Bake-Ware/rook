"""app.*: launcher apps from the PackageManager, activities started from the
background (rook.kit.android.start says when Android may have hidden one)."""

from rook.kit import android
from rook.kit.android import screen as sc
from rook.kit.ctx import KitError

#: app.settings page -> Settings action. Only these pages: a raw action could
#: open any settings screen.
PAGES = {
    "main": "android.settings.SETTINGS",
    "wifi": "android.settings.WIFI_SETTINGS",
    "internet": "android.settings.panel.action.INTERNET_CONNECTIVITY",
    "bluetooth": "android.settings.BLUETOOTH_SETTINGS",
    "airplane": "android.settings.AIRPLANE_MODE_SETTINGS",
    "network": "android.settings.WIRELESS_SETTINGS",
    "nfc": "android.settings.NFC_SETTINGS",
    "display": "android.settings.DISPLAY_SETTINGS",
    "sound": "android.settings.SOUND_SETTINGS",
    "volume": "android.settings.panel.action.VOLUME",
    "dnd": "android.settings.ZEN_MODE_SETTINGS",
    "battery": "android.intent.action.POWER_USAGE_SUMMARY",
    "location": "android.settings.LOCATION_SOURCE_SETTINGS",
    "apps": "android.settings.MANAGE_APPLICATIONS_SETTINGS",
    "app": "android.settings.APPLICATION_DETAILS_SETTINGS",
    "notifications": "android.settings.APP_NOTIFICATION_SETTINGS",
    "accessibility": "android.settings.ACCESSIBILITY_SETTINGS",
    "date": "android.settings.DATE_SETTINGS",
    "storage": "android.settings.INTERNAL_STORAGE_SETTINGS",
    "security": "android.settings.SECURITY_SETTINGS",
    "developer": "android.settings.APPLICATION_DEVELOPMENT_SETTINGS",
}


def launcher_apps() -> list[dict]:
    Intent = android.jclass("android.content.Intent")
    i = Intent(Intent.ACTION_MAIN)
    i.addCategory(Intent.CATEGORY_LAUNCHER)
    pm = android.context().getPackageManager()
    found = pm.queryIntentActivities(i, 0)
    apps = [{"label": str(info.loadLabel(pm)), "package": str(info.packageName)}
            for info in (found.get(k).activityInfo for k in range(int(found.size())))]
    return sorted(apps, key=lambda a: (a["label"].lower(), a["package"]))


def pick_app(apps: list[dict], query: str) -> tuple[dict | None, list[dict]]:
    """The app ``query`` names: an exact name or package (any case), else the
    only name starting with it, else the only name or package containing it.
    (app, candidates); app is None when none or several match."""
    q = query.strip().lower()
    for test in (lambda a: q in (a["label"].lower(), a["package"].lower()),
                 lambda a: a["label"].lower().startswith(q),
                 lambda a: q in a["label"].lower() or q in a["package"].lower()):
        hits = [a for a in apps if test(a)]
        if len({a["package"] for a in hits}) == 1:   # one app, maybe two launcher entries
            return hits[0], hits
        if hits:
            return None, hits
    return None, []


def _open(cap, intent, show, out=None):
    """Start ``intent`` (``app``: who took it); with ``show``, wait for another
    app in front and add the screen."""
    sc.awake(cap)
    out = dict(out or {})
    app = android.handler(intent)
    if app and app != out.get("package"):
        out["app"] = app
    if not show:
        return {**out, **android.start(intent)}
    try:
        b = sc.hid()
    except KitError as e:
        return {**out, **android.start(intent), "screen": {"error": e.message}}
    before, rev = sc.front(b).get("package", ""), sc.rev(b)
    out.update(android.start(intent))
    out["screen"] = sc.after(b, rev, before_app=before)
    return out


def list_apps(ctx, query, limit):
    q = query.strip().lower()
    apps = [a for a in launcher_apps() if q in a["label"].lower() or q in a["package"].lower()]
    return {"apps": apps[:limit], "truncated": len(apps) > limit}


def launch(ctx, app, screen, params=None):
    pm = android.context().getPackageManager()
    i = pm.getLaunchIntentForPackage(app) if "." in app else None
    package = app
    if i is None:
        found, hits = pick_app(launcher_apps(), app)
        if found is None:
            names = "; ".join(f"{a['label']} ({a['package']})" for a in hits[:10])
            raise KitError(f"no single app is called {app!r}: {names}" if hits else f"no app called {app!r}")
        package = found["package"]
        i = pm.getLaunchIntentForPackage(package)
        if i is None:
            raise KitError(f"{package} can't be launched")
    for name, value in (params or {}).items():
        if not isinstance(value, (str, bool, int)):
            raise KitError(f"params.{name}: a string, a whole number or true/false")
        i.putExtra(str(name), value if isinstance(value, (str, bool)) else android.jint(value))
    return _open("app.launch", i, screen, {"package": package})


def open_url(ctx, url, screen):
    return _open("app.open_url", android.intent("android.intent.action.VIEW", url), screen)


def share(ctx, text, subject):
    extras = {"android.intent.extra.TEXT": text, **({"android.intent.extra.SUBJECT": subject} if subject else {})}
    i = android.intent("android.intent.action.SEND", mime="text/plain", extras=extras)
    sc.awake("app.share")
    return android.start(android.jclass("android.content.Intent").createChooser(i, "Share"))


def settings(ctx, page, package):
    if page in ("app", "notifications") and not package:
        raise KitError(f"the {page} page needs package")
    if page == "app":
        i = android.intent(PAGES[page], "package:" + package)
    elif page == "notifications":
        i = android.intent(PAGES[page], extras={"android.provider.extra.APP_PACKAGE": package})
    else:
        i = android.intent(PAGES[page])
    return _open("app.settings", i, False)
