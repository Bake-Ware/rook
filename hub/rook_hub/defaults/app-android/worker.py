"""app-android: apps, links, the share sheet and Settings pages on the phone the Rook Android app runs
on (brief AP2), shipped as a plugin installed by default. app.list, app.launch, app.open_url,
app.share and app.settings are 0.5's (origin/castling rook/plugins/app-android): the same names,
arguments and replies.

Nothing here bounds a call (Bake, 17 November): app.list gives every app unless the caller passes
`limit` (0.5 cut at 200 by default and capped `limit`, `query` and the texts; none of that is kept).

Android 10 and later let an app in the background open a screen only once the person has let it
display over other apps. The app's bridge (systems.bake.rook.core.worker.Bridge) asks for that the
first time a cap needs it, waits for the person, and then opens the screen; refused, the error says
how to allow it. 0.5 opened it anyway and said Android might not have shown it (`may_be_blocked`);
that cannot happen now, so the field never comes. `screen: true` (the screen once the app is up)
needs the screen-reading caps, which come with brief AP5: until then the reply's `screen` says so,
as 0.5's did when it could not read the screen.

Off the app it announces nothing: CAPS is empty wherever Chaquopy's `java` module does not import,
which is everywhere but the app."""

import asyncio

from rook_worker import Cap

try:
    from java import jclass, jint     # Chaquopy's: only in the Rook Android app
except ImportError:
    jclass = None

# app.settings page -> Settings action.
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
NO_SCREEN = "reading the screen comes with the ui caps, which this app does not have yet"


def _check(args, cap, allowed, required=()):
    """Refuse an argument the cap does not take, so it is never silently ignored (C3)."""
    extra = sorted(set(args) - set(allowed))
    if extra:
        raise ValueError(f"{cap} takes no argument {', '.join(extra)}; it takes {', '.join(allowed) or 'none'}")
    for name in required:
        if args.get(name) is None:
            raise ValueError(f"{cap} needs {name}")


def _text(args, name, default=None, empty=True):
    value = args.get(name, default)
    if value is not None and (not isinstance(value, str) or (not empty and not value)):
        raise ValueError(f"{name} must be a{'' if empty else ' non-empty'} string")
    return value


def _flag(args, name, default):
    value = args.get(name, default)
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false")
    return value


def _bridge():
    return jclass("systems.bake.rook.core.worker.Bridge")


def _intent(action, data=None, mime=None, extras=None):
    """An Intent. Extras: bool and str as they are, numbers as Java ints."""
    Intent, Uri = jclass("android.content.Intent"), jclass("android.net.Uri")
    intent = Intent(action)
    uri = Uri.parse(data) if data else None
    if uri is not None and mime:
        intent.setDataAndType(uri, mime)
    elif uri is not None:
        intent.setData(uri)
    elif mime:
        intent.setType(mime)
    for name, value in (extras or {}).items():
        intent.putExtra(name, value if isinstance(value, (bool, str)) else jint(value))
    return intent


def _handler(intent):
    """The package that would take an intent (None when Android asks the person, or hides it)."""
    try:
        found = _bridge().context().getPackageManager().resolveActivity(intent, 0)
        package = str(found.activityInfo.packageName) if found is not None else None
    except Exception:
        return None
    return package if package and package != "android" else None


def _start(cap, intent, out=None, show=False):
    """Open `intent`'s screen; `app` says who took it, and with `show`, `screen` says why it is not read."""
    out = dict(out or {})
    app = _handler(intent)
    if app and app != out.get("package"):
        out["app"] = app
    refused = _bridge().start(intent, cap)
    if refused is not None:
        raise ValueError(str(refused))
    if show:
        out["screen"] = {"error": NO_SCREEN}
    return out


def launcher_apps():
    Intent = jclass("android.content.Intent")
    intent = Intent(Intent.ACTION_MAIN)
    intent.addCategory(Intent.CATEGORY_LAUNCHER)
    packages = _bridge().context().getPackageManager()
    found = packages.queryIntentActivities(intent, 0)
    apps = [{"label": str(info.loadLabel(packages)), "package": str(info.packageName)}
            for info in (found.get(k).activityInfo for k in range(int(found.size())))]
    return sorted(apps, key=lambda a: (a["label"].lower(), a["package"]))


def pick_app(apps, query):
    """The app `query` names: an exact name or package (any case), else the only name starting with it,
    else the only name or package containing it. (app, candidates); app is None when none or several match."""
    q = query.strip().lower()
    for test in (lambda a: q in (a["label"].lower(), a["package"].lower()),
                 lambda a: a["label"].lower().startswith(q),
                 lambda a: q in a["label"].lower() or q in a["package"].lower()):
        hits = [a for a in apps if test(a)]
        if len({a["package"] for a in hits}) == 1:      # one app, maybe two launcher entries
            return hits[0], hits
        if hits:
            return None, hits
    return None, []


def _list(query, limit):
    q = query.strip().lower()
    apps = [a for a in launcher_apps() if q in a["label"].lower() or q in a["package"].lower()]
    if limit is None:
        return {"apps": apps, "truncated": False}
    return {"apps": apps[:limit], "truncated": len(apps) > limit}


async def list_apps(args):
    _check(args, "app.list", ("query", "limit"))
    limit = args.get("limit")
    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 1):
        raise ValueError("limit must be a whole number, 1 or more; leave it out for every app")
    return await asyncio.to_thread(_list, _text(args, "query", ""), limit)


def _launch(app, params, show):
    packages = _bridge().context().getPackageManager()
    intent = packages.getLaunchIntentForPackage(app) if "." in app else None
    package = app
    if intent is None:
        found, hits = pick_app(launcher_apps(), app)
        if found is None:
            names = "; ".join(f"{a['label']} ({a['package']})" for a in hits)
            raise ValueError(f"no single app is called {app!r}: {names}" if hits else f"no app called {app!r}")
        package = found["package"]
        intent = packages.getLaunchIntentForPackage(package)
        if intent is None:
            raise ValueError(f"{package} can't be launched")
    for name, value in params.items():
        intent.putExtra(str(name), value if isinstance(value, (str, bool)) else jint(value))
    return _start("app.launch", intent, {"package": package}, show)


async def launch(args):
    _check(args, "app.launch", ("app", "params", "screen"), ("app",))
    app, show = _text(args, "app", empty=False), _flag(args, "screen", False)
    params = args.get("params") or {}
    if not isinstance(params, dict):
        raise ValueError("params must be an object of strings, numbers and booleans")
    for name, value in params.items():
        if not isinstance(value, (str, bool, int)):
            raise ValueError(f"params.{name}: a string, a whole number or true/false")
    return await asyncio.to_thread(_launch, app, params, show)


async def open_url(args):
    _check(args, "app.open_url", ("url", "screen"), ("url",))
    url, show = _text(args, "url", empty=False), _flag(args, "screen", False)
    return await asyncio.to_thread(lambda: _start("app.open_url", _intent("android.intent.action.VIEW", url), show=show))


def _share(text, subject):
    extras = {"android.intent.extra.TEXT": text, **({"android.intent.extra.SUBJECT": subject} if subject else {})}
    intent = _intent("android.intent.action.SEND", mime="text/plain", extras=extras)
    refused = _bridge().start(jclass("android.content.Intent").createChooser(intent, "Share"), "app.share")
    if refused is not None:
        raise ValueError(str(refused))
    return {}


async def share(args):
    _check(args, "app.share", ("text", "subject"), ("text",))
    return await asyncio.to_thread(_share, _text(args, "text", empty=False), _text(args, "subject", ""))


def _settings(page, package):
    if page in ("app", "notifications") and not package:
        raise ValueError(f"the {page} page needs package")
    if page == "app":
        intent = _intent(PAGES[page], "package:" + package)
    elif page == "notifications":
        intent = _intent(PAGES[page], extras={"android.provider.extra.APP_PACKAGE": package})
    else:
        intent = _intent(PAGES[page])
    return _start("app.settings", intent)


async def settings(args):
    _check(args, "app.settings", ("page", "package"))
    page = _text(args, "page", "main")
    if page not in PAGES:
        raise ValueError(f"page must be one of {', '.join(PAGES)}")
    return await asyncio.to_thread(_settings, page, _text(args, "package", ""))


CAPS = {} if jclass is None else {
    "app.list": Cap(
        "Apps on the phone's launcher, by name: {apps: [{label, package}], truncated}; query keeps those "
        "whose name or package contains it, limit gives only the first that many.",
        {"type": "object", "additionalProperties": False, "properties": {
            "query": {"type": "string", "default": ""}, "limit": {"type": "integer", "minimum": 1}}},
        list_apps),
    "app.launch": Cap(
        "Open an app on the phone by its package (com.android.settings) or its launcher name (Maps); "
        "an unclear name gives an error listing the candidates.",
        {"type": "object", "additionalProperties": False, "required": ["app"], "properties": {
            "app": {"type": "string", "minLength": 1},
            "params": {"type": "object", "description": "Intent extras for the launch: strings, numbers and booleans.",
                       "additionalProperties": {"type": ["string", "integer", "boolean"]}},
            "screen": {"type": "boolean", "default": False}}},
        launch),
    "app.open_url": Cap(
        "Open a link or URI on the phone in the app that takes it: https:, tel: (the dialer, no call), "
        "mailto: and smsto: (drafts, nothing sent), geo:, market:, an app's deep link.",
        {"type": "object", "additionalProperties": False, "required": ["url"], "properties": {
            "url": {"type": "string", "minLength": 1}, "screen": {"type": "boolean", "default": False}}},
        open_url),
    "app.share": Cap(
        "Open the phone's share sheet with text (and a subject, for apps that take one); a person picks where it goes.",
        {"type": "object", "additionalProperties": False, "required": ["text"], "properties": {
            "text": {"type": "string", "minLength": 1}, "subject": {"type": "string", "default": ""}}},
        share),
    "app.settings": Cap(
        "Open a Settings page on the phone: main, wifi, internet, bluetooth, airplane, network, nfc, display, "
        "sound, volume, dnd, battery, location, apps, app (needs package), notifications (needs package), "
        "accessibility, date, storage, security or developer.",
        {"type": "object", "additionalProperties": False, "properties": {
            "page": {"type": "string", "enum": list(PAGES), "default": "main"},
            "package": {"type": "string", "default": ""}}},
        settings),
}
