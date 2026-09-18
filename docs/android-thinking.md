# Android thinking view

Settings → **Show thinking** defaults off and saves immediately. Enabling it
adds `thinking: true` to the protocol-2 hello. Changing it closes/cancels the
current socket before opening a replacement, retaining the conversation ID and
using the existing generation guards to discard predecessor callbacks. Standby
stays in standby. Unknown events and answer IDs are ignored; decisions are
strictly display metadata, including timeout/error/disabled results.

A one-line, muted row appears below the matching assistant message for text or
below the matching user utterance for voice. Tap to expand probabilities,
confidence, score/expected values, model, adapter, calibration, status, and any
error. Turn attachment supports either arrival order and resets on each socket
because server turn IDs restart. Turning the setting off hides existing rows.
Chat remains activity-local, as before; events missed while the activity is
paused are not replayed.

Build/test on cachyrig (existing JDK and SDK; Gradle wrapper 8.7):

```sh
JAVA_HOME=/home/bake/jdk17 ANDROID_HOME=/home/bake/android-sdk \
  ANDROID_USER_HOME="$PWD/android/build/android-home" \
  GRADLE_USER_HOME="$PWD/android/build/gradle-home" \
  ./android/gradlew -p android :app:testDebugUnitTest :app:assembleDebug
```

The build-local Gradle home was seeded from `/home/bake/.gradle`, and the
build-local Android home uses a copy of `/home/bake/.android/debug.keystore`.
Gradle resolves test dependencies and Chaquopy resolves Python packages as
needed. `stageWorker` runs automatically.

Output: `android/app/build/outputs/apk/debug/app-debug.apk`, version **0.4.3 (7)**.
Generate OTA metadata with `python3 android/build_apk_manifest.py`. Follow
[Android updates](android-updates.md#publishing) for publication with the existing
signer and hash allowlist. Manual installation, when authorized, is
`adb -s SERIAL install -r PATH_TO_APK`.
