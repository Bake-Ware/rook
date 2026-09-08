# Enrollment compatibility deployment — 2026-09-08

User authorization: finish code and deploy to bakenetca; hold GitHub pushes until
testing. No existing-band PSK has been changed. Source remains uncommitted on
`master` at `28dea31`; release archives include a per-file SHA-256 manifest.

## Releases and state

- First release, deployed and checked:
  `/opt/rook-releases/enrollment-20260908-ca44cada`.
  Google/local accounts, explicit linking/merge, band permissions, avatars and
  pairing controls. Real Google login and merge into the existing operator
  completed; the operator owns all three imported bands and its avatar loads.
- Second release, deployed:
  `/opt/rook-releases/enrollment-20260908-devices-v1`.
  Adds browser-authorized terminal enrollment, certificate configuration reads,
  renewal/revocation, enrolled worker refresh, generic APK and installer updates.
  Superseded by the relay-fix release below.
- Host: `ubuntu@151.145.63.234` (bakenetcanada). Services: `rook-remote`,
  `rook-band-mcp`; the `telesthete-hub` relay is preserved.
- Shared state: `/var/lib/rook-band-mcp/setup.json` and `enrollment.db`.
  The database includes account sessions, memberships, current/retired band keys,
  device certificates and the private enrollment CA. Protect it and its backups.
- Google credentials: `/var/lib/rook-band-mcp/google-web-client.json` and
  `google-android-debug-client.json`, mode `0600`; never package these in source.
- Service overrides: `/etc/systemd/system/{rook-remote,rook-band-mcp}.service.d/90-enrollment-upgrade.conf`.
  Both services use the release `PYTHONPATH`, shared DB/setup and Google files.
  Original service commands and original source directories remain intact.
- Consistent private backups: `/home/ubuntu/rook-upgrade-20260908/backup`;
  take another SQLite backup before each subsequent schema deployment.

## Artifacts and tests

- `band-worker-enrollment.pyz`: separate compatibility download. The original
  `band-worker.pyz` and signed `band-worker.json` are preserved; existing workers
  do not receive the new bundle through automatic OTA. The local compatibility
  build is unsigned and identified as `115.dirty.gadget`; do not publish it as a
  signed fleet update. Source and artifact hashes identify the exact snapshot.
- Android: `android/app/build/outputs/apk/debug/app-debug.apk`, version `0.2.0`,
  versionCode `2`, package `systems.bake.rook`. No default PSK or voice token.
  The debug signing SHA-1 matches the registered Google Android client:
  `38:55:53:90:61:9B:62:B1:F9:4A:EE:50:22:84:EB:89:21:AA:5E:37`.
  Public `/apk` is enabled only with this artifact's exact SHA-256 in
  `ROOK_PUBLIC_APK_SHA256`; any replacement requires a new verified hash.
- Python suite: `.venv/bin/pytest -q -p no:cacheprovider` — 98 passed, including the relay regression test.
- Worker: compatibility zipapp `--selftest` — 16 plugins, 78 capabilities.
- Local browser: registration, band creation, rolling code, mobile layout and
  no JavaScript errors. Live browser: Google callback, account merge and avatar.
- Android build: `JAVA_HOME=/home/bake/.local/lib/rook-jdk17/jdk-17.0.20.1+1
  ANDROID_HOME=/home/bake/android-sdk ./gradlew :app:assembleDebug --console=plain`.
  Physical phone sign-in, Windows installer execution and ESP32 hardware remain
  untested. The headless-shell browser test is not an Android device test.

## Rollback

For a server-code regression before fleet cutover, point both overrides back to
an inspected compatible release, run `sudo systemctl daemon-reload`, then restart
`rook-band-mcp` and `rook-remote`. Check all services, account login, scoped config
and mesh roster. Preserve the shared live database; restoring an old backup could
resurrect a retired key or lost account access. A rollback to a release without
device endpoints stops enrolled config refresh and eventually its cached lease;
prefer a forward fix when devices depend on the new API. Remove the public APK
hash if rolling back to code that cannot verify the generic artifact.

## Not finished by this release

Certificates authorize HTTPS config retrieval; peer traffic still uses the shared
PSK. Android Keystore/ESP32 unique identities, certificate mesh transport,
revocation enforcement on receivers, CA rotation tooling, staged migration,
new-channel acknowledgments and physical recovery tests remain outstanding.
Use the [migration handoff](HANDOFF-enrollment-upgrade.md); immediate Replace PSK
is not the staged fleet migration mechanism. This describes the initial deployment checkpoint; see the rollout update below.

## Second-release deployment and canary result

The second release is live; all services are active and the public account,
generic APK and compatibility-worker endpoints respond successfully. Pairing
issued a canary device certificate, the downloaded worker hash matched, and
both installer variants omit the permanent PSK. Generated Bash syntax passed.

The command canary exposed a pre-existing WebSocket bridge defect: sharing a
UDP socket prevented two WebSocket peers from receiving each other's packets
because the hub excludes the sender. The follow-up release
`/opt/rook-releases/enrollment-20260908-devices-v2` gives every WebSocket a
separate UDP endpoint. A real-UDP regression test proves request/reply fanout,
band isolation and shutdown. Final canary results will be recorded separately.

### Final result

`enrollment-20260908-devices-v2` is live. All 264 archived source files were
verified against the manifest before restart. `rook-remote`, `rook-band-mcp`
and `telesthete-hub` are active; Rook's MCP roster returned 25 workers after
deployment. Public account/APK/compatibility-worker endpoints return 200 with
the worker user agent; bare `/worker` remains 403. Cloudflare rejects Python's
default urllib user agent, so enrollment explicitly supplies `rook-enrollment/1`.

Live canaries passed:

- Pairing enrolled a unique certificate into exactly the temporary test band.
- Both installer variants omit the PSK; the Bash script parses successfully.
- A real WebSocket worker/controller `info.ping` returned `pong` through the hub.
- Rotating **only the canary band's** PSK caused the enrolled worker to fetch the
  new epoch over its certificate, restart and answer `info.ping` on the new band.
- Revoking the device certificate made the worker leave and exit with code 78.
- Separate terminal browser approval fetched all authorized configurations,
  issued a certificate, renewed it and fetched config with the renewed identity.
  That temporary identity was then revoked; its next config request returned 403.
- The canary band was revoked; the three original active bands are preserved.
  No test worker was started on an original band. Temporary local copies of
  downloaded credentials were removed after revocation.

Artifact SHA-256:

| Artifact | SHA-256 |
| --- | --- |
| Final source archive `rook-source-v3.tar.gz` | `f0f79ff7a5222f655dd1195c5e1dd22e11a95abeb8d625f1c9aa9e1ee9480ee3` |
| Compatibility worker | `8a6eecfe14be11cd05853a8b2fbeefbf13339b43268326cc6cb3f6f9d2785ff3` |
| Generic APK | `41a9a7ca758a79c56d12ecd9e16d70891243fcdbfc3c991559eb979e9248c49b` |

Latest private pre-deploy backup:
`/home/ubuntu/rook-upgrade-20260908/backup-devices-20260908-204020`.
This final result was recorded after deployment; the archived source manifest
still describes the exact pre-deployment source snapshot. GitHub publication followed at the rollout checkpoint below.

## Rollout authorization update

The operator confirmed the APK works and authorized GitHub publication followed
by worker updates and PSK migration. Another agent is actively using the mesh:
worker updates, service restarts and live credential changes must wait for its
completion notification on Bakephone. A failure report keeps the rollout paused
until the operator resolves it. The notification post/read round trip passed;
test ID `rook-gate-20260908-01` must never release the rollout.

The next substantive Rook notification is the completion report; routine worker
and voice-service notifications are excluded. Poll approximately once per minute.
The expected wording is deliberately unspecified by the operator. Review the
report's meaning, and do not turn a failure report into permission to proceed.

## Migration build preparation

The user confirmed the 0.2.0 APK works. The compatibility/account release was
pushed to GitHub as `f5c62c9`. The notification round trip passed, and the next
substantive Rook notification was “Pianobar is ready,” reporting successful
functional and recovery checks. This cleared the rollout gate. The captured
report stays in a private rollout directory, outside GitHub.

The next build adds staged PSK migration and native Android lifecycle support.
It has not yet rotated an existing band. Android 0.3.0 is required for automated
certificate configuration refresh and in-process worker reconnect; the original
native app advertised desktop update/restart operations which cannot safely
re-execute its embedded Python process.

The server's `rook.remote.migrate` command defaults to preflight. Execution
requires an explicit expected-worker inventory and a reviewed completion report.
It enrolls trusted existing workers with public CSRs and CSR-bound grants;
those grants return certificates, never PSKs. A separate private-key proof over
HTTPS obtains configuration. This is routine migration of a trusted fleet,
not a bootstrap method for a compromised mesh.

All expected devices must persist and acknowledge the candidate configuration
before activation. Both band keys stay available while the controller verifies
per-device signed challenges and `info.ping` on the replacement band. Finalizing
retires the old hash and invalidates pairing codes. An overdue or interrupted
migration stops the coordinator and retains authorized connectivity for forward
recovery; it never silently excludes missing devices or rolls a device back to
an earlier credential epoch. Resume uses the same recorded expected set.

New device keys use Ed25519 through the existing PyNaCl/libsodium dependency,
including on Android. Serialization follows RFC 8410 PKCS#8/SPKI and PKCS#10;
tests validate the CSR, key and signatures independently with cryptography.
P-256 identities from the first release remain supported. Android private keys
are in app-private files with backups disabled; hardware Keystore integration
and certificate enforcement on general peer traffic remain separate work.

Migration build validation: 105 tests pass, including staged acknowledgements,
proof/replay checks, revoked devices, emergency rotation, interrupted enrollment
retries, CSR-bound grants, and native Android reconnect/revocation lifecycle.
Live fleet cutover is still pending the deployment canary and compatible apps.
