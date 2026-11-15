# Rook Worker — native Android app (Chaquopy)

A real APK that runs the Rook worker (`rook.core2.worker`, with the phone
plugins under `rook/plugins` that list `android` in `platforms`) as a
**foreground service**, so it survives Doze, and adds the phone's own voice,
widgets, notifications and watch link. The worker Python is bundled into the
APK. How the worker runs and signs in: [docs/castling-phone.md](../docs/castling-phone.md).

## Layout

```
android/
├── settings.gradle / build.gradle / gradle.properties
├── stage_worker.py            # copies Rook 0.5 + telesthete.protocol into the APK python source
└── app/
    ├── build.gradle           # Android + Kotlin + Chaquopy; pip: pynacl aiohttp websockets
    └── src/main/
        ├── AndroidManifest.xml
        ├── java/systems/bake/rook/
        │   ├── MainActivity.kt          # the conversation screen
        │   ├── SettingsActivity.kt      # voice, sign-in, permissions, app
        │   ├── WorkerService.kt         # foreground service -> castling_entry.start()
        │   ├── BootReceiver.kt          # autostart after reboot
        │   ├── PythonHost.kt            # Chaquopy bootstrap
        │   ├── ScreenCaptureBridge.kt   # MediaProjection -> JPEG
        │   └── HidAccessibilityService.kt
        ├── python/
        │   └── castling_entry.py        # status/enroll/start/stop/kick for the service
        └── res/...
```

`app/src/main/python/rook/`, `.../telesthete/` and `castling_plugins.zip` are
**generated** by `stage_worker.py` and git-ignored.

## Build

Prereqs: Android Studio (or the SDK + JDK 17), and a sibling `telesthete`
checkout at `../telesthete/telesthete` (or a pip-installed telesthete).

```bash
# 1. Stage Rook 0.5 + the protocol into the Chaquopy python source set (the build also runs it)
python3 android/stage_worker.py

# 2. Build the debug APK
cd android
./gradlew :app:assembleDebug      # (generate the gradle wrapper first if absent: `gradle wrapper`)

# 3. Install on a connected device
./gradlew :app:installDebug
# APK: android/app/build/outputs/apk/debug/app-debug.apk
```

> `stage_worker.py` needs a python3 with `pyyaml` (it reads the plugin
> manifests to pick the phone plugins).

## Deployment settings

The source names no real server. Everything deployment-specific is read at
build time from `-P` properties, environment variables, or an untracked
`android/rook.properties` (gitignored):

| Setting | Default | What it's for |
|---|---|---|
| `ROOK_SERVER` | `https://rook.example.com` | The APK download link, and the **only** origin the self-updater accepts updates from |
| `ROOK_VOICE_URL` | `wss://voice.example.com/ws` | The voice agent's WebSocket (editable in Settings) |
| `ROOK_WAKE_MODEL` | *(none)* | An openWakeWord model under `app/src/main/assets/wakeword/` (e.g. `private/hey_rook.onnx`); without it voice is tap-to-talk |
| `ROOK_WAKE_PHRASE` | *(none)* | The phrase that model detects, shown in the app |
| `ROOK_ASSISTANT_NAME` | `Rook` | The name shown on the conversation screen |

```properties
# android/rook.properties
ROOK_SERVER=https://rook.yourdomain.com
ROOK_VOICE_URL=wss://voice.yourdomain.com/ws
ROOK_WAKE_MODEL=private/hey_rook.onnx
ROOK_WAKE_PHRASE=hey rook
```

Put private wake-word models in `assets/wakeword/private/` (also gitignored).
The build refuses to embed a voice token. Publish the update feed with
`ROOK_PUBLIC_BASE=<same as ROOK_SERVER> python3 android/build_apk_manifest.py`.

## First run on the device

1. Open **Rook Worker** > Settings > Band and sign in to your hub (its address,
   or scan its sign-in QR with the camera).
2. Tap **Start worker**: a persistent notification appears and the worker joins
   the band.
3. Settings > Permissions lists each grant (screen capture, accessibility for
   `hid.*`, battery optimisation off, ...) with its live state; tap one to grant it.

## Voice and default assistant (0.4.2)

The APK uses a local Silero speech gate before wake-word activation. In a live
conversation, sustained speech can interrupt playback when the platform echo
canceller is enabled; short false interruptions can resume playback. Devices
without an enabled echo canceller retain the explicit Interrupt control.

Settings → **Use Rook as default assistant** opens Android's assistant selector.
System assistant invocation opens the same Rook conversation and microphone
permission flow. This does not grant proprietary hardware hotword support or
make Rook a general dictation provider. Open Rook after an APK replacement to
resume wake-word standby.

The version-2 backend preserves conversation and tool results across reconnects.
Interrupting speech does not cancel background work; explicitly ask to stop a
job when that is intended. See [the voice service](../services/voice/README.md)
for protocol, deployment and acoustic-test limitations.

## Attention: decision cards and the quiet feed

The hub's cards reach the phone as `notification.post` with an `id` and show
as Yes/No decision cards; what was done with them (decided, seen, opened,
dismissed, acknowledged, unlocked) goes back as `notification-android.card`
events. The **Attention** tab lists the grants it needs and the waiting cards. Contract, privacy and rules: [docs/design/attention-phone.md](../docs/design/attention-phone.md).

Settings → App → Stable / Beta picks the APK update channel (stable by default).

## Wear OS: push-to-talk on a watch

`:wear` is a small watch app: opening it (launcher, its tile, or a key you map to it) starts
listening, and a tap ends the conversation or stops a reply. The watch is only a microphone
and a speaker. It streams Opus (PCM where a device has no Opus codec) over a Wear Data Layer
channel (`/rook/voice`) to this app on the paired phone; `WatchBridge` runs a `speech` session
on the hub there, with your hub token (Settings > Band), and sends the reply audio back to play
on the watch's earbuds or glasses ([docs/castling-watch.md](../docs/castling-watch.md)). Shared protocol and
codec code is in `:wearlink`.

The Data Layer only links apps with the same package name and signing key, so the watch APK
keeps `applicationId systems.bake.rook` and must be signed with the phone app's key.

```sh
./gradlew :wear:assembleDebug :app:assembleDebug
# Watch: Settings > About watch > Software > tap Software version until developer mode is on;
# Developer options > ADB debugging + Wireless debugging > Pair new device.
adb pair <watch-ip>:<pair-port>      # enter the code the watch shows
adb connect <watch-ip>:<port>
adb -s <watch-ip>:<port> install -r wear/build/outputs/apk/debug/wear-debug.apk
```
