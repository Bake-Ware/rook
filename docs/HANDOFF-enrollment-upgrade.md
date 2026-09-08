# Enrollment, device identity, and PSK migration handoff

Updated 2026-09-08. The account/Google/pairing compatibility release is live
on bakenetca. The working tree remains uncommitted on top of `28dea31`, and no
GitHub push or existing-band PSK rotation has occurred. The user authorized
server deployment before GitHub publication. See [deployment record](DEPLOYMENT-enrollment.md)
for artifacts, validation, rollout status and rollback.

## Accepted requirements and proposed security model

- Five random words separated by hyphens remain the permanent band PSK. Owners
  can replace or revoke it. Existing opaque PSKs remain valid until migration.
- A separate six-character alphanumeric code rolls on Tokens, expires after
  five minutes, and authorizes enrollment into exactly one band. A worker can
  install using `curl -fsSL 'https://rook.bakeforge.com/worker?band=CODE' | bash`.
  It does not need a Google or local login when using that code.
- Accounts can own multiple bands and invite users to individual bands. Google
  is the first external identity provider; local login remains available, and
  linking/merging requires proof of control of both accounts.
- Android can use Google to discover all authorized band configurations.
  Headless workers can complete enrollment through a browser on another device.
  Google is an enrollment option, not a requirement at every boot.
- Include device certificates in this upgrade, before the final PSK cutover.
  Proposed model: Google/local authorization or a scoped pairing grant permits
  certificate issuance; each device generates its own private key and proves
  possession. Band membership is separately checked and revocable. A claimed
  worker UUID or possession of the legacy PSK does not establish device identity.

The phrase remains a permanent shared band secret, but under the proposed new
protocol it must not be sufficient to impersonate a device. Phrase-only setup
should require owner approval before issuing a device identity. This additional
approval is a proposed security policy, not an already implemented requirement.
Five words from the shipped 7,776-word vocabulary provide about 64.6 bits of
randomness. Certificates alone do not strengthen PSK-derived encryption: the
new transport must establish authenticated ephemeral session keys independent
of the phrase's entropy, using a maintained protocol implementation.

## Current implementation and gaps

| Area | Implemented | Remaining |
| --- | --- | --- |
| PSKs | Five words, stable band IDs, epochs, retired hashes, explicit rotate/revoke | Staged per-band migration and actual device acknowledgments |
| Accounts | Google/local login, explicit linking/merge, owners/members, invitations, scoped config downloads, cached/custom avatars | Native account UI testing; account-scoped mesh/MCP command access is not exposed |
| Enrollment | Six-character rolling codes, browser-authorized terminal fetches, unique P-256 device keys, signed configuration requests, renewal/revocation | Certificate enforcement on peer mesh traffic |
| Installers | Code or Google/local browser authorization; private credential file; enrolled worker refresh/reconnect | Windows execution and Termux dependency validation |
| Android | Generic APK, Google Credential Manager, all authorized configurations, active-band selection, pairing fallback | Physical Google sign-in and upgrade test; Android Keystore device identity/renewal |
| ESP32 | Private PSK header provisioning helper; existing NVS precedence preserved | Unique device identity, authenticated transport, hardware recovery test |

Validation: 98 Python tests pass. A local browser tested account registration,
band creation, pairing countdown and mobile layout. Real Google web login,
explicit local-account merge and Google avatar loading passed on bakenetca.
The new APK builds and the worker bundle passes its offline self-test. Physical
Android/Windows/ESP32 tests and the new certificate mesh transport remain pending.

Device certificates currently authorize **HTTPS configuration retrieval only**.
They last 30 days, renew within seven days of expiry, and use one-use 60-second
signed challenges. Private device keys remain on the worker. The enrollment CA
private key is stored in the protected enrollment database, separate from OTA
signing and public HTTPS keys; include it in consistent private backups. Owner
revocation and member removal block new configuration reads and renewals. The
cooperative enrolled worker checks every 30 seconds; network outages and server
429/5xx responses permit at most a one-hour cached lease, never a known denial.
This is not a bound on access by a hostile legacy PSK holder.

The compatibility worker download is separate from the existing signed OTA
manifest. Installing it does not trigger a fleet-wide update. The following
transport and migration sections are the remaining design/acceptance work,
not a description of shipped mesh enforcement.

The current Replace PSK operation is an immediate controller cutover, not a
fleet migration. Revocation stops these controllers from using the old key;
legacy peers can still talk to each other with that key. Do not describe it as
mesh-wide enforcement or use it as the routine migration mechanism.

## Implementation sequence

### 1. Account and device foundations

Introduce durable accounts, external identities keyed by issuer plus subject,
band memberships, expiring invitations, devices, certificate records, audit
events and migration records. Preserve existing stable band IDs; never use a
short PSK fingerprint as an authorization identity. Bootstrap existing bands
to an explicitly selected initial owner through the existing administrative
access path. Do not let the first public Google login claim them.

Default proposal: owners manage members, secrets and revocation; members can
fetch configuration and enroll devices. Record who authorized each enrollment.
Removal of a member should revoke that member's sponsored devices unless an
owner explicitly transfers them. Account merge must reconcile memberships and
device sponsorship transactionally without silently broadening access.

Apply band authorization to configuration fetching, dashboard API/WebSockets,
Tokens, MCP operations and installer enrollment. The current shared admin login
is not an account system. Design service-to-service authentication between
`rook.bakeforge.com` and the MCP service; do not solve this with an unrestricted
parent-domain session cookie. Preserve an authenticated local recovery path.

### 2. Certificate and transport compatibility release

Create a Rook private certificate authority for device identities. Keep its
signing keys separate from existing worker OTA signing keys and HTTPS server
certificates. Plan protected backups, issuer rotation and public trust-anchor
updates. Devices generate private keys locally; certificates bind the device
public key and stable identity. Membership authorization must identify the band
and current authorization epoch, whether carried in a certificate or a separate
signed, short-lived authorization object.

Choose the transport library through a small interoperability proof on Python,
the telesthete hub and ESP32-S3. Rook currently calls `BandCrypto(psk)` directly
in `rook/worker/transports/telesthete_hub.py`; the firmware also uses a shared
PSK-derived data key. The sibling telesthete specification's session handshake
is not evidence that Rook implements certificate authentication.

Keep the blind relay property: mutually authenticated HTTPS/WSS to a hub secures
that connection but does not supply end-to-end peer identity or encryption.
The selected design must protect relayed peer traffic and authenticate command
senders, authorize their capabilities, prevent replay/downgrades and enforce
membership revocation on receivers. Validate UDP, fragmentation and group
fanout explicitly. Do not invent a certificate-shaped custom handshake or
commit to DTLS/QUIC compatibility before testing the actual libraries/firmware.

ESP-TLS provides client-certificate functionality, but that alone does not
implement the current UDP mesh protocol. See the
[ESP32-S3 ESP-TLS documentation](https://docs.espressif.com/projects/esp-idf/en/latest/esp32s3/api-reference/protocols/esp_tls.html).

Publish concrete certificate lifetimes and offline authorization limits before
rollout. Renewal uses an existing valid device identity and current membership;
it does not require Google at every restart. Expired or revoked identities need
a defined recovery enrollment path. Online revocation must terminate sessions;
offline peers cannot learn revocation instantly, so their authorization lease
must bound stale access. A grace period must never resurrect known revocations.

### 3. Google and native enrollment

Implement web authorization-code login with state, nonce, appropriate PKCE,
validated issuer/audience/signature/expiry and secure local sessions. Identify
Google accounts by verified `sub`, not an editable email address. Linking an
existing local account requires fresh authentication of both identities;
matching emails alone must not merge accounts. Request only `openid email
profile`, with no Google API access scopes. These identity and redirect
requirements follow [Google's OIDC documentation](https://developers.google.com/identity/openid-connect/openid-connect).

#### Google profile avatars (requested 2026-09-08)

Use Google's profile picture as the default avatar for a Rook user created
through Google, or for a linked local user that has no custom avatar. The
existing `profile` scope already covers the optional `picture` claim; no extra
Google API or consent scope is needed. Read it only from a validated ID token
or authenticated UserInfo response whose `sub` matches that identity. See
[Google's claim reference](https://developers.google.com/identity/openid-connect/reference).

Cache a normalized image in Rook and serve it from Rook's avatar endpoint.
Refresh on subsequent successful Google sign-ins when the selected avatar source
is Google, using a bounded refresh interval even if the URL is unchanged. Avatar
fetch failures must not fail login; keep the last good image or show initials.
Track the avatar source (`google`, `custom`, `initials`) separately from bytes;
custom uploads and an explicit initials selection must survive login, linking
and merging. Offer "Use Google photo" to return to automatic updates. Unlinking
Google stops refresh and clears a Google-sourced avatar, leaving custom images
alone. Account deletion removes cached profile images.

Reuse the shared image storage/serving machinery in
`rook/band_mcp/chat_rooms.py` and the dashboard where appropriate, but key human
avatars by the stable Rook account identity (`user:<account-id>`), not email or
the existing shared `user:operator` identity. Enforce account/band visibility on
image retrieval and self-service editing. Fetch only validated Google image
URLs through a restricted downloader: HTTPS, approved image hosts, public
resolved addresses, redirect validation, timeouts and streaming byte limits.
Decode and re-encode a supported raster format with pixel limits and strip
metadata before storage; the current MIME/256-KB check alone is not validation.

Acceptance: first login imports the photo; missing photo and download failure
use the fallback; Google photo updates refresh; custom/initials choices survive
repeat login and explicit account merge; unlink/delete clears the appropriate
image; cross-account edits and unsafe image fetches are rejected. Login, merge,
custom/initials precedence and restricted avatar fetching are implemented.
Account deletion is not currently exposed.

#### Native and headless enrollment

On Android, use Credential Manager and verify its Google ID token on the Rook
backend. Offer authorized band selection; downloading multiple configurations
does not need to imply simultaneous active membership in the Android UI.
On a headless installer, authorize a Rook enrollment transaction in a separate
browser and return a short-lived, device-bound result. Do not assume Google's
restricted device authorization flow is available for arbitrary worker clients.

Keep the short installer URL. The compatibility script exchanges its pairing
grant plus a freshly generated device public key over HTTPS, then persist the
device identity and selected configuration. The current code is reusable during
its five-minute window; it is a temporary bearer grant, not a single-use token.
Expose enrolled devices to the owner. Redact query codes in upstream proxy logs
as well as application logs. A leaked valid code can still enroll a device
within its scope and lifetime; it grants no access to other bands.

For ESP32, a private provisioning image may carry the selected band and a
one-device bootstrap grant; generate the lasting private key on the dongle.
Never distribute the same device private key in a reusable firmware image.
Account for saved NVS overriding build defaults and provide USB recovery.
Retain private PSK-header provisioning for compatibility until cutover.

## Fleet migration runbook

1. **Inventory and recovery.** Record each controller, hub, worker, APK and
   dongle; band, software/protocol version, last seen, update mechanism and
   recovery method. Account for offline devices. Back up configuration and
   databases consistently, including memberships, retired keys, epochs and CA
   material; store backups privately. The installer and MCP must use the same
   `ROOK_ENROLLMENT_DB` and `ROOK_SETUP_PATH` with appropriate file permissions.
2. **Deploy compatibility software while preserving the old PSK.** Ship signed
   worker updates and matched Rook/telesthete versions. Stage Android worker
   sources into the APK and retain the existing APK signing identity for an
   in-place upgrade. Verify the firmware update path independently; the old
   roadmap identifies unsigned dongle OTA. Use trusted local flashing where
   authenticated OTA is unavailable. New code must understand migration state
   before any key changes occur.
3. **Enroll identities.** Each device gets owner-authorized enrollment through
   Google/local access, a scoped code, or an independently trusted existing
   device key. Existing PSK traffic can advertise an upgrade, but cannot prove
   entitlement to a new identity. If the old PSK is compromised, never issue a
   certificate merely because a device can communicate on that band.
4. **Canary.** Prove authenticated configuration retrieval, new transport,
   renewal, revocation and recovery on Linux, Windows, Android and an ESP32.
   Verify real commands and return traffic, not merely a roster announcement.
5. **Prepare a band migration.** Generate a new permanent five-word PSK, retain
   the stable band ID, increment the credential epoch, set an explicit overlap
   deadline and record the expected device set. Keep active and pending epochs
   separately. A credential epoch must be distinct from the existing worker
   config timestamp. Deliver new credentials only through the authenticated
   new channel. Never broadcast the new PSK over the old shared-key band.
6. **Stage and acknowledge.** Persist each device's progress as
   `pending -> fetched -> staged -> new-channel-verified -> confirmed`.
   Checkpoints survive controller and worker restarts; requests are idempotent.
   Confirm over the new authenticated connection and verify a useful command
   round trip. Show offline, failed and recovery-required devices on the webapp.
7. **Cut over.** For a routine, non-compromise migration, allow only bounded
   old/new compatibility while staging. Do not relay sensitive new-protocol
   traffic into the legacy band. Retire the old epoch once the expected devices
   are confirmed or explicitly marked for recovery. Enforce the new protocol
   at controllers and receivers, terminate legacy sessions, revoke old pairing
   grants and remove legacy credentials from active config and rollback files.
8. **Recover late devices.** An enrolled device may fetch the current config
   over its valid authenticated HTTPS identity. An unenrolled or expired device
   requires owner-authorized enrollment or local recovery. Its appearance must
   never reopen the legacy band or reimport a retired PSK.

The existing `rook/worker/wconfig.py` commit-confirmed mechanism can restore
the previous config after a timeout. Extend it to respect a durable minimum
credential epoch and retired-key state. Do not use the current
`rook_config_apply` to carry a replacement key through a compromised old band.

### Emergency compromise and rollback

Emergency revoke is separate from routine migration: immediately revoke the
affected PSK/grants and device identities as appropriate, reject the compromised
epoch, and require trusted re-enrollment. No overlap or automatic fallback to
the compromised credential. Old unupgraded peers may continue their own legacy
communication until updated, isolated or physically recovered.

Before cutover, a failed canary can return to an old credential only if it is
still explicitly authorized and not compromised. After cutover, software
rollback must preserve the minimum accepted epoch, current credentials and
revocation history. Do not restore a pre-revocation database wholesale or roll
back to a binary that ignores the new enforcement. Recovery must move forward
to trusted credentials. Maintain compatible signed recovery artifacts.

## Google setup needed from the operator

Update, 2026-09-08: the Google project, web client, Android debug client and
initial test user have now been configured. See
[Google OAuth development configuration](GOOGLE-AUTH-SETUP.md) for the exact
IDs, protected local credential paths, verification performed and remaining
production/release-signing work. The table below describes the required inputs;
it does not mean the completed inputs need to be requested again.

| Input | Proposed value / action |
| --- | --- |
| Google Cloud project | Reuse an appropriate existing project or create one for Rook; provide project ID and OAuth client IDs |
| Consent configuration | Rook branding, authorized domain, audience and initial test accounts |
| Web OAuth client | Register proposed callback `https://rook.bakeforge.com/auth/google/callback`; exact match required; callback implemented and tested live |
| Web origin | `https://rook.bakeforge.com` if required by the selected browser integration |
| Client secret | Store only in protected server configuration; do not paste into chat, commit it, or package it in installers/APKs |
| Android OAuth client | Same Cloud project, package `systems.bake.rook`, SHA-1 of the actual APK signing certificate; register debug/release variants as needed |
| Existing Android signing identity | Locate the existing release keystore or signed APK so its public fingerprint can be obtained and in-place updates preserved; no private key upload needed |
| Initial owner | Google account and/or existing local administrator to receive the imported bands; confirm through authenticated bootstrap |

Android needs an Android OAuth client and uses the web client ID as its backend
audience; see [Google's Android sign-in codelab](https://codelabs.developers.google.com/sign-in-with-google-android).
If Android App Links are selected, configure their separate domain association
and SHA-256 signing fingerprint. Firebase is not required by this design.
Implementation and mocked-provider testing can start before Cloud configuration
is ready; live web/Android sign-in testing requires configured clients and a
test account. No external certificate vendor is needed for the proposed device CA.

## Release acceptance and remaining decisions

- [ ] Review and commit the final source after testing. The user authorized an
      uncommitted server snapshot and explicitly held GitHub pushes; record
      source/artifact hashes and test/build commands for each deployed release.
- [ ] Prove two users/two bands cannot cross-fetch, enroll, administer, invoke
      commands or receive streams outside their membership, including MCP.
- [ ] Test Google login, local recovery, invitations, explicit linking/merge,
      logout, malicious callbacks, wrong audience and replayed tokens.
- [ ] Test code expiry, global/per-source limits across services, concurrent
      enrollment/revocation, scoped enrollment and proxy log redaction.
- [ ] Demonstrate a holder of only the legacy PSK cannot join the enforced new
      band, decrypt its traffic or obtain a device certificate.
- [ ] Test individual device revocation, member removal, certificate expiry,
      renewal, CA rotation, bad device clocks and offline authorization limits.
- [ ] Test migration interruptions at every checkpoint, controller restarts,
      duplicates, offline devices and explicit recovery exclusions. A timeout,
      old environment variable, config backup or NVS value must not revive a
      retired credential.
- [ ] Exercise canaries on all four platforms; verify physical ESP32 recovery
      and a signed APK upgrade preserving app data. Public APK/build artifacts
      must contain no band secrets or shared device private keys.
- [ ] Agree the certificate/transport implementation after the compatibility
      proof, and set concrete overlap, renewal and offline lease durations.
- [ ] Confirm owner/member permissions and whether Android needs multiple
      simultaneously active bands. Neither blocks the initial account schema.

Handoff order: account/device schema and transport proof; compatible clients;
Google and pairing-backed certificate enrollment; staged migration controls;
physical canaries; scheduled per-band cutover. Complete the identity enforcement
before relying on five-word PSKs as the fleet's new permanent credentials.
