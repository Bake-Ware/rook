# Microphone lifetime fix — 0.4.6 (10)

Static analysis confirmed that ending a voice session closed only its socket.
Manual sessions armed standby even with wake disabled, standby started capture
unconditionally, and exhausted reconnect retries retained it. Backgrounding had
no idle release path. The recorder lived inside its thread, so stopMic could
only set a flag and wait; a blocked read could outlive the stop request. Effect
cleanup exceptions could skip recorder release and audio-mode restoration.

Capture now belongs to an active voice session or explicitly enabled wake
standby. Session end (including idle timeout), exhausted retries, backgrounding
without a voice session, and saving wake OFF release unused capture. The service
can stop the live recorder to unblock read; its capture thread releases it.
AEC, echo/noise, recorder, inference, speaker routing and mode cleanup are
independently protected. Idle capture also drops the microphone foreground
service and wake lock; a service without a session or wake standby stops itself.
Typed chat does not start capture. Thinking-setting socket replacement preserves
an active voice session. Saving wake ON can restart standby with mic permission.
The wake preference default remains true.

Master incorporates the published 0.4.3–0.4.5 UI and the 7622389 activity-progress
follow-up: thinking display, voice picker, compact controls, tabs, status strip,
and expandable decision-marked chat are retained.

Validation: full Python suite (237 passed); Android JVM regression suite covers
session end, disabled standby, exhausted retries, active/background/text capture,
wake ON/OFF, and failures in every cleanup stage. Build and test both Android
variants with `:app:test :app:assembleRelease`. No ADB or device validation is
part of this release. Static defects are confirmed; the exact trigger observed
on the S25 Plus and camera/video-call coexistence still require owner testing.

Build the release variant, zipalign, and sign using the existing OTA certificate
(local Android debug keystore, certificate SHA-256
`975e7c23158e28f2ed4161b1672e3f011fd1b5e9ecfbb501bc9333b99596c863`).
Verify it against a full download of the prior 0.4.5 APK. Generate the sidecar
with `android/build_apk_manifest.py`, copy the active bakenetcanada web release,
replace only its APK/sidecar, and switch its WorkingDirectory and public hash
allowlist using the prior release procedure. Validate the public `/apk.json`
and full `/apk` download using User-Agent `rook-worker` before notifying Bake.

Manual checklist:
1. Install the same-signer update over the current app (retain app data).
2. Turn wake OFF and save. Start and end a voice session. Confirm the microphone
   indicator clears, camera video records sound, and a video call gets audio.
3. Background Rook and repeat the camera and video-call checks.
4. Turn wake ON and save; confirm wake standby works. Holding the mic in this
   mode is intentional. Turn wake OFF and save; confirm the mic is released.
