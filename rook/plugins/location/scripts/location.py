"""location.*: systems.bake.rook.LocationBridge for a fix; Maps intents for places."""

from urllib.parse import quote

from rook.kit import android
from rook.kit.android import screen

TRAVEL = {"drive": "driving", "walk": "walking", "bike": "bicycling", "transit": "transit"}


def get(ctx, timeout):
    return android.result(android.app("LocationBridge").get(android.context(), float(timeout)))


def _open(cap, url):
    screen.awake(cap)
    i = android.intent("android.intent.action.VIEW", url)
    app = android.handler(i)
    return {**({"app": app} if app else {}), **android.start(i)}


def navigate(ctx, to, mode):
    return _open("location.navigate", "https://www.google.com/maps/dir/?api=1&destination=" + quote(to, safe="")
                 + "&travelmode=" + TRAVEL[mode] + "&dir_action=navigate")


def show(ctx, to):
    return _open("location.show", "geo:0,0?q=" + quote(to, safe=""))
