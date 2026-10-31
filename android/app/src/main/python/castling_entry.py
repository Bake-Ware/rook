"""Chaquopy entrypoint for Rook 0.5 (Castling) in the Android app.

The app runs either the 0.4 worker (worker_entry) or this one; the person
picks in Settings > Band > Rook version (0.4 is the default). Everything 0.5
keeps lives in ``<files>/rook05``: the worker config, the anchor, the data
folder and the phone plugins, unpacked from the APK.

The Kotlin side (Castling.kt, WorkerService.kt) calls, off the main thread:

    castling_entry.status(files_dir)                              # JSON: signed in? hub, url, owner, name
    castling_entry.enroll(files_dir, target, name, listener, force)   # blocks until approved, refused or cancelled
    castling_entry.cancel()                                       # ends a sign-in that waits for approval
    castling_entry.start(files_dir)                               # blocks until stop()
    castling_entry.stop()
    castling_entry.kick(reason)                                   # the default network changed

``target`` is the hub's address or the text of its sign-in QR
(``rook://enroll?url=…&code=…&hub=…``, which the phone's camera hands the
app as a link). Without a one-time code the hub gives a short code; the
listener's ``say(text)`` shows it so the person can approve it on the
dashboard. Enrollment is rook.core2.enroll.device, the same as
``python -m rook.core2.worker enroll``.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import pkgutil
import shutil
import threading
import zipfile
from pathlib import Path

log = logging.getLogger("rook.android.castling")

HOME = "rook05"
#: The phone plugin folders, zipped by stage_worker.py: the 0.5 loader reads
#: plugin folders from disk, and Chaquopy serves Python from inside the APK.
PLUGINS_ZIP = "castling_plugins.zip"

_cancel = threading.Event()


class Cancelled(Exception):
    pass


def home(files_dir: str) -> Path:
    return Path(files_dir) / HOME


def config_path(files_dir: str) -> Path:
    return home(files_dir) / "worker.yaml"


def _config(files_dir: str) -> Path:
    """The worker config; a first one names the plugin folder. Enrollment
    adds the anchor, data folder, name and worker id."""
    import yaml
    path = config_path(files_dir)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(yaml.safe_dump({"plugins": ["plugins"], "log": {"level": "info"}}), encoding="utf-8")
    return path


def _plugins_zip() -> bytes:
    data = pkgutil.get_data(__name__, PLUGINS_ZIP)
    if not data:
        raise RuntimeError(f"{PLUGINS_ZIP} is missing from this APK (stage_worker.py makes it)")
    return data


def unpack_plugins(files_dir: str, data: bytes | None = None) -> Path:
    """Unpack the APK's phone plugins into ``rook05/plugins`` when they
    differ from what is there (a new APK brings new plugins)."""
    data = _plugins_zip() if data is None else data
    target = home(files_dir) / "plugins"
    stamp = hashlib.sha256(data).hexdigest()
    if (target / ".stamp").is_file() and (target / ".stamp").read_text() == stamp:
        return target
    fresh = target.with_name("plugins.new")
    shutil.rmtree(fresh, ignore_errors=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        z.extractall(fresh)
    (fresh / ".stamp").write_text(stamp)
    shutil.rmtree(target, ignore_errors=True)
    fresh.rename(target)
    log.info("phone plugins unpacked (%s)", stamp[:12])
    return target


def status(files_dir: str) -> str:
    """``{"enrolled": bool, "name", "worker_id", "hub", "url", "owner"}`` as JSON."""
    import yaml
    from rook.core2.enroll import device
    path = config_path(files_dir)
    out: dict = {"enrolled": False}
    if not path.exists():
        return json.dumps(out)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        out.update(name=raw.get("name"), worker_id=raw.get("worker_id"))
        out["enrolled"] = device.home_of(path) is not None
        done = path.parent / (raw.get("data") or "data") / "enrollment.json"
        if done.is_file():
            got = json.loads(done.read_text(encoding="utf-8"))
            out.update({k: got.get(k) for k in ("hub", "url", "owner")})
    except (OSError, ValueError, AttributeError, yaml.YAMLError) as e:
        out["error"] = f"can't read the 0.5 config: {e}"
    return json.dumps(out)


def _sleep(seconds: float) -> None:
    if _cancel.wait(seconds):
        raise Cancelled()


def enroll(files_dir: str, target: str, name: str, listener=None, force: bool = False) -> str:
    """Sign this phone in with its 0.5 home hub. Returns status() JSON with
    ``ok``, or ``{"ok": false, "error"}``."""
    from rook.core2.enroll import device
    _cancel.clear()
    say = listener.say if listener is not None else log.info
    try:
        path = _config(files_dir)
        url, code, pinned = device.parse_target((target or "").strip())
        device.check_url(url)       # https, or a hub on this phone itself (adb reverse in tests)
        if not force and device.home_of(path):
            raise device.EnrollError(device.already(device.home_of(path)))
        bundle = device.sign_in(url, name, code, pinned, say=say, sleep=_sleep)
        if _cancel.is_set():
            raise Cancelled()
        device.install(path, bundle, url, force=force)
    except Cancelled:
        return json.dumps({"ok": False, "error": "sign-in cancelled"})
    except device.EnrollError as e:
        return json.dumps({"ok": False, "error": str(e)})
    log.info("enrolled as %s with hub %s", bundle["name"], bundle["hub"]["hub_id"])
    return json.dumps({"ok": True, **json.loads(status(files_dir))})


def cancel() -> None:
    _cancel.set()


def start(files_dir: str) -> None:
    """Run the 0.5 worker until stop(); refuses before sign-in."""
    from rook.core2.enroll import device
    from rook.core2.worker import android
    path = _config(files_dir)
    if not device.home_of(path):
        raise RuntimeError("this phone isn't signed in to a Rook 0.5 hub (Settings > Band)")
    unpack_plugins(files_dir)
    log.info("0.5 worker starting from %s", path)
    android.start(str(path))
    log.warning("0.5 worker exited")


def stop() -> None:
    from rook.core2.worker import android
    android.stop()


def kick(reason: str = "") -> bool:
    from rook.core2.worker import android
    log.info("redial: %s", reason)
    return android.kick()
