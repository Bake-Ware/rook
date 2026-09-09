# Android APK updates (0.4.1 / version code 5)

Install 0.4.1 manually once on devices running an earlier APK. In Rook Settings,
scroll to **App updates** and allow installation from Rook. Leave notifications
enabled so Android can ask for confirmation when needed. Automatic updates are
on by default and can be disabled with the checkbox. The browser download
button remains at the bottom as a fallback.

The foreground worker checks the official `/apk.json` feed after startup and
then every six hours. `device.update` queues an immediate check/install and
returns without holding a band RPC open. `device.update_status` reports the
installed/target version, progress, last check, installation permission, and
required action. Automatic checks stop with the worker. Turning automatic
updates off does not cancel an already queued update.

Only the fixed HTTPS rook.bakeforge.com feed is accepted. Downloads have bounded
size and time, and must match the manifest's SHA-256 and size. Before committing,
Rook checks package name, strictly increasing Android version code, and the
same signing certificate as the installed app. PackageInstaller independently
verifies the APK signature. Certificate rotation is not supported by this
updater; a differently signed package is rejected.

Android 12+ is asked to install without interaction. This is conditional on
Android's rules, not an override of them. If install-source permission is
missing, Rook posts an **Allow Rook updates** notification. If PackageInstaller
requires confirmation, Rook posts **Install Rook update**, which opens Android's
confirmation UI when tapped. Older Android versions use that confirmation flow.
If notifications are disabled, Settings/update_status still report the pending
action and the browser download remains available.

A committed session is remembered to avoid duplicate installs. Sessions older
than 24 hours are abandoned before retrying. After APK replacement,
MY_PACKAGE_REPLACED clears update state and restarts the worker if autostart is
still enabled, preserving the existing band settings and stable worker ID.

## Publishing

Build the universal APK with the existing signing certificate, then run:

```
python3 android/build_apk_manifest.py
```

Publish `app-debug.apk` as `rook/remote/rook-worker.apk` and the generated
`rook-worker-apk.json` beside it in a new server release. Set
`ROOK_PUBLIC_APK_SHA256` to the manifest hash when switching the release.
`/apk.json` is public only with this allowlist and fails closed if the approved
APK or manifest does not match. Always advance both versionName and versionCode
in `android/app/build.gradle`. The ROOK_APK_VERSION_CODE/NAME Gradle properties
are available for isolated upgrade tests; never publish their test artifacts.
Desktop worker update artifacts are independent and need not change for APK-only
releases.

## Verification

On an isolated Android 14 emulator, version code 5 accepted a same-signer test
APK at code 6 and rejected wrong-version metadata and a differently signed APK.
With installation permission denied, the callback posted a notification and its
tap opened Android's permission prompt. With permission granted, the update
installed silently. The worker rejoined the isolated test band at code 6 under
the same worker ID and with its foreground service running. No physical phones
were changed during this test.

The test-only instrumentation modes `verify` and `install` run through
`NativeSmokeInstrumentation`; APK fixtures live in private test files. The
server tests cover absent, mismatched, and approved update manifests and reject
an artifact modified after approval.

Android API rules:
https://developer.android.com/reference/android/content/pm/PackageInstaller.SessionParams#setRequireUserAction(int)
