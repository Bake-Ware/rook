# Rook Worker — native Android app (Chaquopy)

A real APK that runs the `rook.worker` band worker as a **foreground service**,
so it survives Doze far better than the Termux path, and adds two native backends
Termux can't do well:

- **`screenshot.capture`** → real screen capture via **MediaProjection**
  (the Termux build could only take a *camera* photo).
- **`hid.*`** → taps / swipes / text / back-home-recents via an
  **AccessibilityService** — **no root required**.

It connects to the same band as every other worker: `mcp.example.com:443 --ws`
with the band PSK. No `band-worker.pyz` download — the worker Python is bundled
into the APK.

## Layout

```
android/
├── settings.gradle / build.gradle / gradle.properties
├── stage_worker.py            # copies rook.worker + telesthete.protocol into the APK python source
└── app/
    ├── build.gradle           # Android + Kotlin + Chaquopy; pip: pynacl aiohttp websockets
    └── src/main/
        ├── AndroidManifest.xml
        ├── java/systems/bake/rook/
        │   ├── MainActivity.kt          # settings + grant screen/HID/battery
        │   ├── WorkerService.kt         # foreground service -> worker_entry.start()
        │   ├── BootReceiver.kt          # autostart after reboot
        │   ├── PythonHost.kt            # Chaquopy bootstrap
        │   ├── ScreenCaptureBridge.kt   # MediaProjection -> JPEG
        │   └── HidAccessibilityService.kt
        ├── python/
        │   ├── worker_entry.py          # start()/stop(); swaps in native plugins
        │   └── rook_android/plugins/    # screen.py (MediaProjection), hid_a11y.py (AccessibilityService)
        └── res/...
```

`app/src/main/python/rook/` and `.../telesthete/` are **generated** by
`stage_worker.py` and git-ignored.

## Build

Prereqs: Android Studio (or the SDK + JDK 17), and a sibling `telesthete`
checkout at `../telesthete/telesthete` (same layout `build_band_worker.py` expects).

```bash
# 1. Stage the worker + protocol into the Chaquopy python source set
python3 android/stage_worker.py

# 2. Build the debug APK
cd android
./gradlew :app:assembleDebug      # (generate the gradle wrapper first if absent: `gradle wrapper`)

# 3. Install on a connected device
./gradlew :app:installDebug
# APK: android/app/build/outputs/apk/debug/app-debug.apk
```

> Re-run `stage_worker.py` whenever `rook.worker` or `telesthete.protocol` change.

## First run on the device

1. Open **Rook Worker**. The hub/PSK prefill from `BuildConfig` defaults — edit
   if needed, set a worker name.
2. Tap **Start worker** → a persistent notification appears and the worker joins
   the band (verify with `rook_workers`).
3. Optional grants (each is one tap, then a system dialog):
   - **Grant screen capture** → enables `screenshot.capture`.
   - **Enable HID (accessibility)** → enables `hid.*`. Toggle "Rook Worker" on in
     the Accessibility settings screen that opens.
   - **Ignore battery optimization** → strongly recommended so Android doesn't
     kill the service.

## Capability parity

| Capability        | Termux build            | This app                          |
|-------------------|-------------------------|-----------------------------------|
| `shell.*`         | ✅ Termux shell          | ✅ (app sandbox shell)            |
| `file.*`          | ✅                       | ✅ (app-scoped storage)           |
| `info.*`          | ✅                       | ✅                                |
| `screenshot.*`    | camera photo only       | ✅ real screen (MediaProjection)  |
| `hid.*`           | needs root              | ✅ rootless (AccessibilityService)|
| persistence       | termux-services + Boot  | ✅ foreground service + BootReceiver |

## Notes / TODO

- `ScreenCaptureBridge` grabs a single frame per call (create/destroy a
  VirtualDisplay each time) — fine for occasional screenshots; for streaming,
  keep the VirtualDisplay + ImageReader alive.
- MediaProjection consent does not survive an app kill; the service restart will
  need a re-grant (Android 14 keeps it for the session). Consider persisting via
  a re-prompt on next launch.
- `hid.mouse.move` is intentionally unimplemented (no cursor on touch); callers
  get "unknown capability".
- `shell.exec` runs in the app's restricted sandbox — far less capable than the
  Termux environment. If you need a full userland, keep the Termux worker too;
  both can be on the band at once under different names.
