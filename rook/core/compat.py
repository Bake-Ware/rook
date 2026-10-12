"""The oldest peers this release still talks to.

Code that only exists to serve peers older than these may be removed; code
for peers at or above them stays. A "build" is the worker/hub build number
(``git rev-list --count``, stamped into ``rook.worker._build_info.BUILD``).

* ``MIN_WORKER_BUILD`` (pyz workers, and the worker embedded in the APK):
  build 142, the oldest worker known to be deployed (the one embedded in
  the 0.4.1 APK). It predates worker-side ``caps.describe(prefix=)``
  and deauth v2, so the hub keeps filtering describe prefixes itself and
  keeps wrapping deauth orders in a v1 body. Pyz workers update themselves
  over OTA; the APK does not, so this floor moves only when the oldest
  installed APK does.
* ``MIN_ANDROID_APP``: the oldest APK still in use (0.4.1). A fallback for a
  cap the newest APK still lacks stays, whatever its build (for example
  ``work.sessions`` for workers without ``sessions.list``).
* ``MIN_HUB_BUILD``: hubs the ``rook band`` CLI and workers may meet (the
  same release: ``/api/band/whoami`` and ``/api/band/overview`` exist, and
  the hub sends deauth v2 orders).

Never covered by these numbers, so always kept: the ESP32 and RP2040
firmware (frozen; announces without facts, tiers or authz), and code that
reads files or databases written by an older release.
"""

MIN_WORKER_BUILD = 142
MIN_ANDROID_APP = "0.4.1"
MIN_HUB_BUILD = 230

__all__ = ["MIN_ANDROID_APP", "MIN_HUB_BUILD", "MIN_WORKER_BUILD"]
