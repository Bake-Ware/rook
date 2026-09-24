# Android thinking view

Settings → **Show thinking** defaults off and saves immediately. Enabling it
adds `thinking: true` to the protocol-2 hello. Changing it closes/cancels the
current socket before opening a replacement, retaining the conversation ID and
using the existing generation guards to discard predecessor callbacks. Standby
stays in standby. Unknown events and answer IDs are ignored; decisions are
strictly display metadata, including timeout/error/disabled results.

In 0.4.5, a tiny triangle marks a chat bubble only when its turn has a decision.
Tap the bubble to expand details in place. The Decisions tab keeps the full list
and links back to each message. See [Chat / Activity / Decisions](android-activity.md)
for attachment, unread badges, and the status strip. Turning Show thinking off
hides decision markers/details and displays a settings explainer in that tab.

Build/test with a local JDK 17 and Android SDK (Gradle wrapper 8.7):

```sh
JAVA_HOME=$HOME/jdk17 ANDROID_HOME=$HOME/android-sdk \
  ANDROID_USER_HOME="$PWD/android/build/android-home" \
  GRADLE_USER_HOME="$PWD/android/build/gradle-home" \
  ./android/gradlew -p android :app:testDebugUnitTest :app:assembleDebug
```

The build-local Gradle home was seeded from `~/.gradle`, and the
build-local Android home uses a copy of `~/.android/debug.keystore`.
Gradle resolves test dependencies and Chaquopy resolves Python packages as
needed. `stageWorker` runs automatically.

Output: `android/app/build/outputs/apk/debug/app-debug.apk`, version **0.4.5 (9)**.
Generate OTA metadata with `python3 android/build_apk_manifest.py`. Follow
[Android updates](android-updates.md#publishing) for publication with the existing
signer and hash allowlist. Manual installation, when authorized, is
`adb -s SERIAL install -r PATH_TO_APK`.

## Compact conversation UI and voices (0.4.4 / 8)

The top bar holds the title, muted APK version, status, and accessible Talk,
Sleep, and Settings icons. Chat fills the space down to the input row; the
conversation heading and separate Interrupt button are removed. Talk retains
its previous interrupt action while thinking/speaking, and speech barge-in is
unchanged. Worker, voice, and posted-message notification bodies return to the
existing main activity; update permission/installer actions remain intact.

Settings fetches `/voices` over HTTPS on the configured voice host, preserving
its port and using the same bearer token and optional insecure TLS setting.
Known Kokoro prefixes receive friendly labels; unknown IDs remain readable.
Selecting a voice persists it and updates an active socket immediately. Each
connection sends the selected voice after hello. Failed fetches show the saved
voice (or `af_heart`) and a reload action. Saving endpoint settings reloads the
list. Thirteen unit tests cover decisions, catalog parsing, labels, fallback,
and endpoint construction. No emulator screenshot was taken: no emulator was
running and `/dev/kvm` was unavailable on this host.
