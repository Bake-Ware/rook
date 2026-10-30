# Castling 0.5 on the phone

The Rook Android app is a worker: its foreground service runs Python under
Chaquopy, and in 0.5 that Python is the ordinary 0.5 worker
(`rook.core2.worker`) with the phone plugins. Nothing in the app changes
yet; this page says what runs and what the app will need to switch over.

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

## What the app needs to switch over

1. **Stage 0.5 into the APK.** `android/stage_worker.py` copies `rook/worker`
   and `rook/core` today; it also needs `rook/core2`, `rook/kit`,
   `rook/streams` and the phone plugin folders. Chaquopy serves Python from
   inside the APK, but the 0.5 loader reads plugin folders from disk
   (plugin.yaml, scripts, docs): extract them (Chaquopy's `extractPackages`,
   or copy them into the app's files at first start) and name that folder
   under `plugins:` in the config.
2. **Python packages.** Add `pyyaml` and `jsonschema` to the Chaquopy `pip`
   block (pynacl is there). Recent jsonschema pulls in `rpds-py`, a Rust
   extension Chaquopy may have no wheel for: pin `jsonschema<4.18` if the
   build can't find one.
3. **Enrollment.** The app signs the phone in through
   `rook.core2.enroll.device` (`sign_in` with the QR's one-time code, else
   the short code the person approves; then `install`, which writes the
   anchor, the home band and the config). That replaces the 0.4 pairing
   code and Google sign-in flow (castling-enrollment).
4. **The service.** `WorkerService.kt` calls `rook.core2.worker.android`
   (`start`, `stop`, `kick`) instead of `worker_entry`. The 0.4 extras go:
   worker.restart and worker.status become core's, the Workers tab's roster
   reads hub announces, and APK updates become `worker.update`.
5. **Permissions.** `call.dial` places calls, so the manifest needs
   `android.permission.CALL_PHONE` and the app's permission screen should ask
   for it. Everything else the plugins use is already granted today.
6. **Retire the 0.4 plugins** in `rook_android/plugins` and the attention
   service (sorting rules, feed, poll): the hub's attention job takes over
   (castling-attention).

## Tests

Off the phone, with Chaquopy's `java` module faked:

```sh
rook-pytest tests/plugins/test_port_phone.py tests/test_core2_worker_android.py -q
```

`tests/plugins/phone_fakes.py` hands out one MagicMock per Java class, a
context whose permissions and content provider queries answer from the
test, and intents as plain data.
