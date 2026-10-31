"""The Android app's Rook 0.5 side (android/app/src/main/python/castling_entry.py)
as a real process: the phone signs in to an owner hub with a device code the
owner approves, unpacks the phone plugins the APK carries, and runs the 0.5
worker the way WorkerService does. Off the phone there is no Java, so the
phone caps answer "not an Android host": that they answer at all shows the
call reached the phone plugins."""
from __future__ import annotations

import json
import re
import sys

from harness import REPO, START_SECS, Band, Proc, until
from test_enrollment_e2e import InitHub

APP_PY = REPO / "android" / "app" / "src" / "main" / "python"

# What the service does: castling_entry.enroll on the settings screen, then
# castling_entry.start on the worker thread. The APK carries the plugin zip
# that stage_worker.py makes; here the driver makes the same zip.
DRIVER = """
import sys
sys.path.append(sys.argv[3])
import castling_entry, stage_worker
data = stage_worker.plugins_zip(stage_worker.phone_plugins())
castling_entry._plugins_zip = lambda: data

class Listener:
    def say(self, text):
        print("SAY " + text, flush=True)

files, url = sys.argv[1], sys.argv[2]
print("STATUS " + castling_entry.status(files), flush=True)
print("ENROLLED " + castling_entry.enroll(files, url, "testphone", Listener()), flush=True)
castling_entry.start(files)
"""


def test_the_app_signs_in_with_a_device_code_and_runs_the_phone_plugins(request, tmp_path):
    b = Band(tmp_path)
    request.node._e2e_band = b
    try:
        b.start_relay()
        home = InitHub(b, "home", "bake")
        bake = home.start()
        files = tmp_path / "files"                    # the app's files dir
        files.mkdir()
        env = {**b.env, "ANDROID_ROOT": "/system",    # every Android app process has it: os is android
               "PYTHONPATH": f"{b.env['PYTHONPATH']}:{APP_PY}"}
        env.pop("TERMUX_VERSION", None)
        phone = Proc("phone", [sys.executable, "-c", DRIVER, str(files), home.url, str(REPO / "android")],
                     tmp_path, env, tmp_path / "phone.log")
        b.procs.append(phone)

        line = lambda tag: next((ln for ln in phone.lines if ln.startswith(tag)), None)   # noqa: E731
        assert json.loads(until(lambda: line("STATUS "), START_SECS, "status")[7:]) == {"enrolled": False}
        said = until(lambda: line("SAY "), START_SECS, "the code to approve")
        code = re.search(r"approve code ([A-Z]{4}-[A-Z]{4}) on hub ([0-9a-f]{16})", said)
        assert code[2] == home.hub_id
        assert bake.post("/enroll/approve", {"code": code[1]})["ok"]
        enrolled = json.loads(until(lambda: line("ENROLLED "), START_SECS, "enrollment")[9:])
        assert enrolled["ok"] and enrolled["enrolled"], enrolled
        assert (enrolled["name"], enrolled["hub"], enrolled["owner"]) == ("testphone", home.hub_id, "person:bake")

        until(lambda: any(w["name"] == "testphone" and w["online"] for w in bake.get("/workers")["workers"]),
              30, "the hub to see the phone")
        caps = until(lambda: (bake.get("/caps?worker=testphone").get("workers", {}).get("testphone") or {})
                     .get("caps"), 30, "the phone's caps")
        assert {"sms.send", "call.dial", "battery-android.battery.status", "notification-android.notification.post"} <= set(caps), sorted(caps)
        got = bake.call("battery.status", worker="testphone")
        assert not got["ok"] and "not an Android host" in got["error"], got
        assert (files / "rook05" / "plugins" / "battery-android" / "plugin.yaml").is_file()
    finally:
        b.close()
