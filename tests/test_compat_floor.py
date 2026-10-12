"""The supported-peer floor (rook/core/compat.py) stays coherent with what
this tree ships."""
import re
from pathlib import Path

from rook.core import compat

ROOT = Path(__file__).resolve().parents[1]


def _ver(s):
    return tuple(int(x) for x in s.split("."))


def test_floor_is_coherent_with_the_shipped_android_app():
    gradle = (ROOT / "android" / "app" / "build.gradle").read_text(encoding="utf-8")
    shipped = re.search(r'ROOK_APK_VERSION_NAME"\) \?: "([0-9.]+)"', gradle).group(1)
    assert _ver(compat.MIN_ANDROID_APP) <= _ver(shipped)
    assert compat.MIN_WORKER_BUILD > 0 and compat.MIN_HUB_BUILD > 0
