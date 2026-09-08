# Google OAuth development configuration

Configured in Google Cloud on 2026-09-08. The web account system is deployed on
bakenetca; actual Google login, explicit merge with the existing local operator,
ownership of the three imported bands, and the cached Google avatar were tested.
Android Google enrollment builds successfully; phone testing remains pending.

## Project and consent

- Project display name: `rook`
- Project ID: `rook-bakeforge`
- Project number: `238138754704`
- [Google Auth Platform clients](https://console.cloud.google.com/auth/clients?project=rook-bakeforge)
- Audience: External; publishing status: Testing.
- The operator's signed-in Google account is registered as a test user and
  support/developer contact. The verified Rook account has been explicitly merged
  with the existing operator and owns the three imported bands.
- App name: `rook`; home page: `https://rook.bakeforge.com`.
- Authorized domain: `bakeforge.com`.
- Scopes: `openid`, `https://www.googleapis.com/auth/userinfo.email`,
  `https://www.googleapis.com/auth/userinfo.profile`. No sensitive or restricted
  scopes were added.

The configured `profile` scope also supplies Google's optional profile-picture
URL for Rook user avatars. No additional Cloud configuration is needed. Avatar
import, refresh, custom-image precedence and fallback behavior are included in
the [upgrade handoff](HANDOFF-enrollment-upgrade.md#google-profile-avatars-requested-2026-09-08).

## Web client

- Name: `rook web`
- Client ID: `238138754704-u34fdjh5l0inv0lhqqpub9lnddlc55p3.apps.googleusercontent.com`
- JavaScript origin: `https://rook.bakeforge.com`
- Redirect URI: `https://rook.bakeforge.com/auth/google/callback`
- Downloaded client configuration, including the secret, is stored locally at
  `/home/bake/.config/rook/oauth/google-web-client.json` with mode `0600` in a
  mode `0700` directory. The server copy is
  `/var/lib/rook-band-mcp/google-web-client.json`, mode `0600`, loaded through
  `ROOK_GOOGLE_CLIENT_FILE`. Do not put its secret into browser or APK code.

Use this web client ID as the Android backend audience as well. Implement the
callback at the exact registered URI, with state/nonce and token validation.
See [Google's OIDC documentation](https://developers.google.com/identity/openid-connect/openid-connect).

## Android debug client

- Name: `rook android debug`
- Client ID: `238138754704-c1q98mkmdqtn9nrv7qbcki3l3k90fp5c.apps.googleusercontent.com`
- Package: `systems.bake.rook`
- Signing certificate SHA-1:
  `38:55:53:90:61:9B:62:B1:F9:4A:EE:50:22:84:EB:89:21:AA:5E:37`
- Fingerprint extracted from the signing certificate in the existing local
  `android/app/build/outputs/apk/debug/app-debug.apk`, subject `CN=Android Debug`.
  This does not establish the signing identity of APKs already on other devices.
- Downloaded configuration:
  `/home/bake/.config/rook/oauth/google-android-debug-client.json`, mode `0600`.

Register a separate Android client for a different release/Play signing
certificate before distributing a build signed with it. Preserve the existing
installed app's signing identity for in-place upgrades. See the
[Google Android sign-in codelab](https://codelabs.developers.google.com/sign-in-with-google-android).

## Verification and remaining work

Read back both clients' settings, the test-user list, and the saved scopes.
The actual web flow completed Google consent, the registered callback, token
exchange, Rook login and an explicit account merge. The cached avatar loaded in
Chrome. Tests additionally cover signed JWT validation, wrong issuer/audience,
nonce and state binding, replay, membership isolation and avatar precedence.
Android Credential Manager and APK installation still need a physical phone test.

Google's Audience page still reports incomplete branding. Privacy-policy and
terms-of-service URLs are unset; no nonexistent policy pages were registered.
Verification Center reports verification is not required while the app remains
in Testing. Finish the app's real public branding/policy pages and any required
domain verification before production publication. Additional development
accounts can be added through Audience > Test users as needed.

Continue with the [enrollment upgrade handoff](HANDOFF-enrollment-upgrade.md).
