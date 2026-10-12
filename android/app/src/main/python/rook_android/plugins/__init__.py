"""Native Android plugins, discovered by the worker's plugin host.

``worker_entry`` adds this package to the host's discovery next to the stock
``rook.worker.plugins``. Each plugin here is placed with
``place(ANDROID_APP)`` (the APK's worker reports ``app="android"``) and the
stock plugins it replaces (screenshot, hid, battery, selfupdate) are placed
away from it, so on the phone exactly one of each loads, with status,
settings and collision safety like any other plugin.
"""
