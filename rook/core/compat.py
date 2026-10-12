"""The oldest peers this release still talks to.

Code that only exists to serve peers older than these may be removed; code
for peers at or above them stays. A "build" is the worker/hub build number
(``git rev-list --count``, stamped into ``rook.worker._build_info.BUILD``).

* ``MIN_WORKER_BUILD`` (pyz workers): the 2026-09-30 permissions and typed
  settings release, which made ``caps.describe(prefix=)``, deauth v2 orders,
  announce ``facts``/``tiers``/``authz``, ``worker.settings_report`` and
  ``<agent>-history.read_snapshot`` standard. Workers update themselves over
  OTA.
* ``MIN_ANDROID_APP``: the first APK whose embedded worker is at least
  ``MIN_WORKER_BUILD``. The APK updates on its own schedule and lags the pyz,
  so a fallback for a cap the newest APK still lacks stays, whatever its
  build (for example ``work.sessions`` for workers without ``sessions.list``).
* ``MIN_HUB_BUILD``: hubs the ``rook band`` CLI and workers may meet (the
  same release: ``/api/band/whoami`` and ``/api/band/overview`` exist, and
  the hub sends deauth v2 orders, wrapped in a v1 body until this release).

Never covered by these numbers, so always kept: the ESP32 and RP2040
firmware (frozen; announces without facts, tiers or authz), and code that
reads files or databases written by an older release.
"""

MIN_WORKER_BUILD = 230
MIN_ANDROID_APP = "0.4.7"
MIN_HUB_BUILD = 230

__all__ = ["MIN_ANDROID_APP", "MIN_HUB_BUILD", "MIN_WORKER_BUILD"]
