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

## Publication verification

Source commit: `8134e30` on master; bundled worker: `177.foggy.llama`.
All 237 Python tests and all 32 Android tests in **each** of debug and release
passed. `assembleRelease` and its required release lint checks passed. The final
build excluded the unrelated untracked dongle plugin with a build-only staging
hook; its source file and all other pre-existing untracked work remain untouched.
The build is the non-debuggable universal release variant (all four ABIs).

Published download: https://rook.bakeforge.com/apk
OTA manifest: https://rook.bakeforge.com/apk.json
Size: **143,091,363 bytes**. SHA-256:
`32426319715dd0d24919337ad89495ddeaac9d25cc743d9beffa953b00c93759`.
The signature matches the complete downloaded public 0.4.5 APK. The publication
script fetched the full new APK through its public HTTPS URL and verified both
size and hash. The public manifest matches the signed local artifact.

Active web release: `/opt/rook-releases/apk-mic-20260921-8134e30`.
A recursive diff confirmed only the APK and sidecar differ from the prior active
web release. Both web and MCP services are healthy. Existing server code,
separate MCP deployment and desktop-worker artifacts were preserved. Rollback:
`sudo /var/backups/rook/apk-mic-20260921-8134e30/rollback.sh` on bakenetcanada.
The same backup directory contains release metadata, the exact two-file diff,
prior/candidate overrides and the completed public verification record.
No device installs or checks were triggered; owner verification is pending.
