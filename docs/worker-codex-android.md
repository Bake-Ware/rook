# Codex history and Android worker

## Coding sessions

Workers with `$CODEX_HOME/sessions` (default `~/.codex/sessions`) advertise
`codex-history.pull`, `.read`, `.search`, `.analyze`, `.export`, `.resume`, and
`.resumed`: the same operations as `claude-history.*`. Select Claude Code or
Codex on the web Sessions page; all capabilities also work through Rook MCP
and the terminal capability browser. A worker restart discovers newly installed
agent histories. The CLI and its login must exist on the worker's machine.

Codex resume starts `codex resume UUID --no-alt-screen` in a managed PTY in the
session's recorded directory. It sends no model prompt and preserves the host's
Codex approval configuration. Use `proc.read`, `proc.write`, `proc.signal`, and
`proc.close` with the returned handle. The web resume result provides those
controls. This is a local Codex process, not Claude Remote Control or a newly
published remote app-server endpoint. Managed resume state is in memory, as
with Claude; independently launched processes are not tracked by this plugin.

Rollout UUIDs and unique prefixes are accepted. Ambiguous prefixes fail closed.
Response messages are used when present, with event-only transcript fallback;
duplicate event messages and reasoning records are excluded. JSON exports use
the normalized transcript format. Tool usage includes function/custom tool
calls. History operations never read Codex credentials or configuration.

CLI resume reference: https://learn.chatgpt.com/docs/codex/cli

## Android release identity and UI

APK version **0.4.1**, Android version code **5**, appears on the conversation
and Settings screens. The worker announce and `worker.status` include
`app_release: {platform: "android", version: "0.4.1", code: 5}`. MCP worker lists
and the web roster carry this separately from the embedded worker version/build.
The web worker badge shows APK version/code; its tooltip shows worker build.
Older APKs without this field continue showing their worker version.

The app uses the web palette: dark olive surfaces, amber controls, outlined
panels, compact monospace labels. Settings groups connection, device,
permissions, and voice controls. The browser APK download button at the bottom of Settings opens
https://rook.bakeforge.com/apk in the default browser; installation remains the
normal Android package upgrade flow. From 0.4.1, automatic updates and
`device.update` are also available; see [APK updates](android-updates.md). Existing Google enrollment and band
settings persist when upgrading with the same signing certificate.

## Location and find-device

`location.get(timeout=8)` requests GPS/network location itself, with a bounded
1–25 second timeout and listener cleanup. It works without Maps running.
A recent cached fix (15 seconds) avoids a new request; older fixes are returned
with age and a stale flag when over 120 seconds. Device Location must be enabled.
On Android 10+, Settings → Background location guides the user through
**Allow all the time**; grant ordinary location first, then background access,
and stop/start the worker. The worker uses the location foreground-service type
only after the grant. No continuous location collection is performed.
The result contains a Google Maps URL, and the web capability result displays
a clickable Maps link with stale-location labeling.

`device.find(seconds=30)` loops the default alarm at maximum **alarm-stream**
volume for 1–120 seconds. `device.find_stop` stops it immediately. The previous
alarm volume is restored on timeout, stop, or playback failure. A second find
replaces the prior ring. Settings also offers Stop find-device ring. Android's
Do Not Disturb policy and device audio restrictions still apply.

Android location API reference: https://developer.android.com/reference/android/location/LocationManager

## Verification

Python regression tests cover Codex capability parity, parsing, ambiguity,
export, duplicate resume serialization, and app metadata through announce,
MCP, and HTTP. The browser harness checks the Codex picker, APK badge, and Maps
link. `NativeSmokeInstrumentation` exercises alarm volume, timed/manual cleanup,
and location through the Chaquopy capability bridge on an isolated Android 14
emulator with Maps stopped and a synthetic GPS fix injected. This does not
replace a physical-device GPS, audio, or Google sign-in check.
