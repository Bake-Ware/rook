"""location: where the phone the Rook Android app runs on is, and showing or navigating to a place in Maps
(brief AP3), shipped as a plugin installed by default. location.get, location.navigate and location.show
are 0.5's (origin/castling rook/plugins/location and the app's LocationBridge.kt): the same names,
arguments and replies.

location.get answers with the app closed and the screen off, so it needs Android's background location:
the first call asks for location (fine and coarse together, as Android 12 and later want them) and then,
on Android 10 and later, for "Allow all the time", each with Android's own screens, through the app's
bridge (systems.bake.rook.core.worker.Bridge). Refused, the error says where to allow it. It asks the
phone's providers for a new fix and gives the first that comes, as 0.5 did once its known fix was over
15 seconds old; 0.5's shortcut of giving a known fix up to 15 seconds old without asking is not kept, so
every call asks the phone for where it is now. `stale` says the fix given is older than STALE seconds,
as 0.5's did. The app's foreground service takes the type location once Rook may have location all the
time, so Android gives it fixes as it would an app in front.

Nothing here bounds a call (Bake, 17 November). 0.5 waited at most `timeout` seconds for a fix, 8 by
default and never more than 25; `timeout` is still an argument a caller may pass, with no default and no
cap: without it, location.get waits for a fix as long as it takes; with it, a call that gets none in
time gives the freshest fix known, as 0.5 did. 0.5's length caps on places are gone.

Off the app it announces nothing: CAPS is empty wherever Chaquopy's `java` module does not import,
which is everywhere but the app."""

import asyncio
import queue
from urllib.parse import quote

from rook_worker import Cap

try:
    from java import cast, dynamic_proxy, jclass     # Chaquopy's: only in the Rook Android app
except ImportError:
    jclass = None

STALE = 120             # seconds: a fix older than this is reported stale
TRAVEL = {"drive": "driving", "walk": "walking", "bike": "bicycling", "transit": "transit"}
FINE, COARSE = "android.permission.ACCESS_FINE_LOCATION", "android.permission.ACCESS_COARSE_LOCATION"
BACKGROUND = "android.permission.ACCESS_BACKGROUND_LOCATION"
OFF = "Location is off on the phone: turn it on in Settings > Location, then call again"


def _check(args, cap, allowed, required=()):
    """Refuse an argument the cap does not take, so it is never silently ignored (C3)."""
    extra = sorted(set(args) - set(allowed))
    if extra:
        raise ValueError(f"{cap} takes no argument {', '.join(extra)}; it takes {', '.join(allowed) or 'none'}")
    for name in required:
        if args.get(name) is None:
            raise ValueError(f"{cap} needs {name}")


def _bridge():
    return jclass("systems.bake.rook.core.worker.Bridge")


def _age(fix):
    """Seconds since the fix was taken."""
    return max(0.0, (int(jclass("android.os.SystemClock").elapsedRealtimeNanos()) - int(fix.getElapsedRealtimeNanos())) / 1e9)


def _listener(fixes):
    """A LocationListener putting each fix on the queue `fixes`."""
    class Listener(dynamic_proxy(jclass("android.location.LocationListener"))):
        def onLocationChanged(self, fix):
            if fix is not None and not hasattr(fix, "getLatitude"):     # the List<Location> form (Android 12)
                fix = fix.get(fix.size() - 1) if fix.size() else None
            if fix is not None:
                fixes.put(fix)

        def onFlushComplete(self, code):
            pass

        def onProviderEnabled(self, provider):
            pass

        def onProviderDisabled(self, provider):
            pass

        def onStatusChanged(self, provider, status, extras):
            pass
    return Listener()


def _get(timeout):
    bridge = _bridge()
    refused = bridge.askAll([FINE, COARSE], "location.get")
    if refused is not None and not bridge.granted(COARSE):        # approximate alone still gives a fix
        raise ValueError(str(refused))
    sdk = int(jclass("android.os.Build$VERSION").SDK_INT)
    if sdk >= 29:
        refused = bridge.ask(BACKGROUND, "location.get")
        if refused is not None:
            raise ValueError(str(refused))
    Context, Manager = jclass("android.content.Context"), jclass("android.location.LocationManager")
    manager = cast(Manager, bridge.context().getSystemService(Context.LOCATION_SERVICE))
    if sdk >= 28 and not manager.isLocationEnabled():
        raise ValueError(OFF)
    enabled = manager.getProviders(True)
    providers = [str(enabled.get(i)) for i in range(enabled.size()) if str(enabled.get(i)) != Manager.PASSIVE_PROVIDER]
    if not providers:
        raise ValueError(OFF)
    fixes = queue.Queue()
    listener, asked, failure, best = _listener(fixes), 0, None, None
    try:
        for provider in providers:
            try:
                manager.requestLocationUpdates(provider, 0, 0.0, listener, jclass("android.os.Looper").getMainLooper())
                asked += 1
            except Exception as e:      # a precise provider with approximate permission only
                failure = str(e)
        if not asked:
            raise ValueError(f"no location provider answers: {failure}")
        try:
            best = fixes.get(timeout=timeout)       # a new fix; timeout None: as long as it takes
        except queue.Empty:                         # none in time: the freshest one known, as 0.5 gave
            for provider in providers:
                try:
                    fix = manager.getLastKnownLocation(provider)
                except Exception:
                    continue
                if fix is not None and (best is None or _age(fix) < _age(best)):
                    best = fix
    finally:
        manager.removeUpdates(listener)
    if best is None:
        raise ValueError(f"no fix in {timeout} seconds; call again with a longer timeout, or none to wait for one")
    age = _age(best)
    lat, lon = float(best.getLatitude()), float(best.getLongitude())
    return {"lat": lat, "lon": lon, "accuracy_m": float(best.getAccuracy()) if best.hasAccuracy() else None,
            "provider": str(best.getProvider()), "age_s": age, "stale": age > STALE,
            "maps_url": f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"}


async def get(args):
    _check(args, "location.get", ("timeout",))
    timeout = args.get("timeout")
    if timeout is not None and (not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0):
        raise ValueError("timeout must be a number of seconds above 0; leave it out to wait for a fix as long as it takes")
    return await asyncio.to_thread(_get, timeout)


def _open(cap, url):
    """Open `url` in the app that takes it (Maps): {app} names that app."""
    bridge = _bridge()
    intent = jclass("android.content.Intent")("android.intent.action.VIEW", jclass("android.net.Uri").parse(url))
    out = {}
    try:
        found = bridge.context().getPackageManager().resolveActivity(intent, 0)
        app = str(found.activityInfo.packageName) if found is not None else None
    except Exception:
        app = None
    if app and app != "android":
        out["app"] = app
    refused = bridge.start(intent, cap)
    if refused is not None:
        raise ValueError(str(refused))
    return out


def _place(args):
    value = args.get("to")
    if not isinstance(value, str) or not value:
        raise ValueError("to must be a place: an address, a place name or lat,lon")
    return value


async def navigate(args):
    _check(args, "location.navigate", ("to", "mode"), ("to",))
    to, mode = _place(args), args.get("mode", "drive")
    if mode not in TRAVEL:
        raise ValueError(f"mode must be one of {', '.join(TRAVEL)}")
    url = ("https://www.google.com/maps/dir/?api=1&destination=" + quote(to, safe="") + "&travelmode=" + TRAVEL[mode]
           + "&dir_action=navigate")
    return await asyncio.to_thread(_open, "location.navigate", url)


async def show(args):
    _check(args, "location.show", ("to",), ("to",))
    return await asyncio.to_thread(_open, "location.show", "geo:0,0?q=" + quote(_place(args), safe=""))


CAPS = {} if jclass is None else {
    "location.get": Cap(
        "Where the phone is: {lat, lon, accuracy_m, age_s, stale, provider, maps_url}; timeout (seconds) gives up "
        "waiting for a new fix, without it it waits as long as it takes.",
        {"type": "object", "additionalProperties": False, "properties": {
            "timeout": {"type": "number", "exclusiveMinimum": 0, "description": "Seconds to wait for a fix."}}},
        get),
    "location.navigate": Cap(
        "Start turn-by-turn directions on the phone to a place in Maps (an address or a place name).",
        {"type": "object", "additionalProperties": False, "required": ["to"], "properties": {
            "to": {"type": "string", "minLength": 1},
            "mode": {"type": "string", "enum": list(TRAVEL), "default": "drive"}}},
        navigate),
    "location.show": Cap(
        "Show a place on the phone's map (an address, a place name or \"lat,lon\").",
        {"type": "object", "additionalProperties": False, "required": ["to"], "properties": {
            "to": {"type": "string", "minLength": 1}}},
        show),
}
