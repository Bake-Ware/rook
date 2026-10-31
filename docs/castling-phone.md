# Castling 0.5 on the phone

The Rook Android app is a worker: its foreground service runs Python under
Chaquopy, and in 0.5 that Python is the ordinary 0.5 worker
(`rook.core2.worker`) with the phone plugins, chosen in Settings > Band (0.4
stays the default). This page says what runs, how the app switches, and what
is still 0.4.

## What runs

- **The worker**: `rook.core2.worker.android`, the 0.5 runtime with three
  differences. It launches no processes (the phone plugins reach Android
  through Java, never a shell). It installs no Python packages (Chaquopy
  builds them into the APK, so a plugin with `requires.python` is refused at
  install). It has no signals: the service calls

  ```python
  from rook.core2.worker import android
  android.start(files_dir + "/rook/worker.yaml")   # blocks, on the service's worker thread
  android.stop()                                    # any thread; a stop during start-up ends it once up
  android.kick()                                    # the default network changed: redial every band
  ```

  The config is the normal worker config (docs/castling-e2e.md). Android
  sets `ANDROID_ROOT` in every app process, so the worker reports os
  `android` and loads only `platforms: [android]` plugins (Termux is
  `termux`, a different host).

- **The plugins**: the 17 folders in `rook/plugins` with `platforms: [android]`:
  sms, contact, call, clock, location, calendar, phone, screen-android,
  ui-android, hid-android, app-android, clipboard-android, sound-android,
  media-android, notification-android, voice-android, battery-android. The
  table in castling/coverage.md ("The phone ports") says what changed from
  0.4 and why.

- **The Java helpers**: `rook.kit.android` (the app's context, permissions,
  content provider queries, the Kotlin bridges' JSON replies, activity starts
  with Android 10's background-start note) and `rook.kit.android.screen` (the
  accessibility service's screen summary, waiting for the screen to settle,
  passive keep-awake and the PIN unlock). Plugin code is `def cap(ctx, **args)`;
  the host runs it in a thread, so blocking Java calls are fine. Every
  failure is a `KitError` a person can act on.

The plugins call the same Kotlin bridges 0.4 does (`systems.bake.rook.*`:
SpeakBridge, AttentionStore, AttentionChannels, NotificationNavigation,
RookNotificationListener, HidAccessibilityService, KeepAwake, UnlockPin,
WakeActivity, FindDeviceBridge, LocationBridge, ScreenCaptureBridge), so the
Kotlin side needs no change to serve them.

## Events

The phone emits three events, from `every:` hooks that keep a cursor in
the plugin's data folder (a restart picks up what arrived meanwhile; the
very first run announces nothing old):

| Event | When | Taints |
|---|---|---|
| `sms.received` | a text arrived (30 s) | yes |
| `notification-android.received` | another app posted a notification (30 s) | yes |
| `notification-android.card` | the person answered, acked, dismissed, saw or opened a card (10 s) | no |

Until events travel to the hub (`TODO(castling:events)` in the worker
runtime) they reach only this worker's `on:` hooks.

## The app's 0.4/0.5 switch

The app runs 0.4 or 0.5, never both: Settings > Band > **Rook version**.
0.4 is the default, and nothing about 0.4 changed (its pairing, Google
sign-in, hub/key fields, the stable and beta update channels). Switching
stops the running worker; Start runs the chosen one.

- **Signing in (0.5).** Settings > Band shows "Sign in to your hub": the hub's
  address and this phone's name. Sign in asks the hub for a device code and
  shows it ("approve code BCDF-GHJK on hub …"); the person approves it on the
  dashboard (`GET /enroll/pending`, `POST /enroll/approve`). Or the person
  scans the hub's sign-in QR with the phone's camera: the
  `rook://enroll?url=…&code=…&hub=…` link opens the app, which names the hub
  and asks before it signs in (one-time code, hub id pinned). Signing in again
  says the new hub becomes home and every other band goes. Plain `http://` is
  refused except to the phone itself (`adb reverse` in testing).
- **What runs.** `castling_entry.py` (Chaquopy) is the 0.5 entry, beside the
  0.4 `worker_entry.py`. It enrolls with `rook.core2.enroll.device` (as
  `python -m rook.core2.worker enroll` does), and its `start(files_dir)`
  unpacks the phone plugins and runs `rook.core2.worker.android`. Everything
  0.5 keeps is in the app's `files/rook05/`: `worker.yaml`, `anchor.json`,
  `data/` (bands.json, enrollment.json, the replay cache) and `plugins/`.
  `WorkerService.kt` calls `start`, `stop` and `kick(reason)` on whichever
  entry the switch names.
- **The APK.** `android/stage_worker.py` stages `rook/core2` (less `hub` and
  `migrate`), `rook/kit`, `rook/streams`, the manifest schema
  (`rook/_conformance`) and zips every plugin folder with `android` in
  `platforms` into `castling_plugins.zip` (the 0.5 loader reads folders from
  disk; the app unpacks them into `files/rook05/plugins` when the zip
  changes). Chaquopy's pip block gains `pyyaml` and `jsonschema<4.18`
  (4.18+ needs rpds-py, a Rust wheel Chaquopy lacks). `CALL_PHONE` is in the
  manifest for `call.dial`, with a "Phone calls" row on the Permissions tab
  in 0.5.

### Still on 0.4, and what each needs for 0.5

The phone's own features keep working in 0.5 mode, but they still talk to
0.4 services:

| Feature | Today (both modes) | For 0.5 |
|---|---|---|
| Voice (conversation, wake word, Talk button) | the 0.4 voice server's WebSocket (Settings > App) | a client for the hub's `speech` plugin (PR #287): `speech.open` / `speech.hear` with 16 kHz audio clips, `speech.read` for replies; the URL and key from the enrollment bundle's `services` (`data/services.json`) instead of typed settings |
| Watch (Wear OS) | streams its mic to the phone, which relays to the 0.4 voice server | nothing on the watch: it follows the phone's voice client |
| Widgets (Talk, Waiting, Chat) | Talk and Chat use the 0.4 voice session; Waiting reads the 0.4 attention cards | Talk and Chat follow the voice client; Waiting reads the hub's cards, which reach the phone as `notification.post` with an `id` (castling-attention) |
| Decision cards, attention feed | the 0.4 attention service and sorting rules | the hub's attention job; answers already travel back as `notification-android.card` events once events reach the hub (`TODO(castling:events)`) |
| APK updates | the 0.4 site's feed, stable or beta | `worker.update` from the hub |
| Workers tab roster | 0.4 hub roster | the hub's announces |

Until then the 0.4 bits in `rook_android/plugins` stay: they are the 0.4
worker's.

## Tests

Off the phone, with Chaquopy's `java` module faked:

```sh
rook-pytest tests/plugins/test_port_phone.py tests/test_core2_worker_android.py \
    tests/test_android_castling_entry.py tests/e2e/test_phone_app_e2e.py -q
```

`tests/e2e/test_phone_app_e2e.py` runs `castling_entry` as the app does
(sign in with a device code a real hub's owner approves, then start) and calls
a phone cap through the hub.

`tests/plugins/phone_fakes.py` hands out one MagicMock per Java class, a
context whose permissions and content provider queries answer from the
test, and intents as plain data.
