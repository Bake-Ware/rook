# Plugins

A plugin is a folder with a `plugin.yaml` (its **manifest**) and, if it
needs code, a few Python scripts. The manifest declares every cap the
plugin offers and what each one is (risk, scope, effects, arguments), so
the hub knows a plugin's caps before any worker runs it. Plugins don't
bundle the software they drive: the `torrent` plugin drives Deluge, it
doesn't ship it. Plugins only talk to each other through caps.

Some plugins run on workers (shell, files, devices), some on the hub (the
workbench, agents, notify), and a plugin can have parts on both.

## The hub's library

The hub's **library** (`library:` in `hub.yaml`) is the folder of plugins
it trusts, one folder per plugin. At start the hub validates each one,
refuses any with errors (they are logged), and runs their hub parts. The
library is read again at every push, so a new version is just new files
there.

## Installing a plugin on a worker

Owners push a plugin from the library to one worker:

```sh
curl -s -H "$H" -d '{"worker":"kaiju","plugin":"torrent","why":"Manage torrents on kaiju."}' \
  http://127.0.0.1:8740/plugins/push
```

What happens:

1. The hub validates the manifest, resolves the plugin's Python
   requirements for that worker (a conflict refuses the install with the
   reason instead of breaking the worker), fetches its large assets and
   checks their hashes.
2. It signs the list of the plugin's file hashes **for that worker**
   (`plugin.sig`) and writes a **change record** for the push
   ([change](../../rook/plugins/changes/docs/manual.md)): its script sends
   the bundle with the cap `core.plugin.install`, dispatched and journaled
   like any call, and `core.plugin.rollback` is its way back
   (`change.revert`). The reply names the change (`"change": "r_…"`). A hub
   without the change plugin in its library refuses pushes.
3. The worker installs it only when the signature is from a hub that may
   approve plugins there (its home hub or a co-owner) and every file
   matches. It loads the new version **without restarting** (nothing
   running on the machine is killed), keeps the previous version, and runs
   the plugin's `check` caps.
4. A failing check rolls the plugin back by itself. To go back by hand:

```sh
curl -s -H "$H" -d '{"worker":"kaiju","cap":"core.plugin.rollback","args":{"plugin":"torrent"},"why":"The new version breaks pause."}' \
  http://127.0.0.1:8740/call
```

Installed plugins load again when the worker restarts, re-checked. A
worker's own `plugins:` folders (in `worker.yaml`) load without a push:
they are trusted because the machine's owner put them there.

A plugin's content (personas, skills, docs, pages) is read from where the
hub holds the plugin: the agents plugin reads every installed plugin's
personas and skills (`hub.manifests`), page plugins their pages.

## Every plugin

The plugins that ship with 0.5, by package. Each has its own manual page,
linked.

**Core** (the hub itself; always there):

- [approval](../../rook/core2/hub/doors/web/builtin/approval/docs/manual.md): held calls and your answers.
- [hub](../../rook/core2/hub/doors/web/builtin/hub/docs/manual.md): the hub and its workers.
- [journal](../../rook/core2/hub/doors/web/builtin/journal/docs/manual.md): searching the journal.
- [llm](../../rook/core2/hub/doors/web/builtin/llm/docs/manual.md): which language models the hub can use.
- [pages](../../rook/core2/hub/doors/web/builtin/pages/docs/manual.md): the hub site's pages.
- [setting](../../rook/core2/hub/doors/web/builtin/setting/docs/manual.md): hub and per-person settings.
- [backup](../../rook/core2/hub/backup/docs/manual.md): sealed backups of the hub that only the recovery key opens, and restoring one on a new machine.

**Core services** (install on the hub):

- [notify](../../rook/plugins/notify/docs/manual.md): reach a person on the channel that reaches them.
- [telegram](../../rook/plugins/telegram/docs/manual.md) and [discord](../../rook/plugins/discord/docs/manual.md): the band's bots.
- [llm-anthropic](../../rook/plugins/llm-anthropic/docs/manual.md), [llm-openai](../../rook/plugins/llm-openai/docs/manual.md), [llm-cli](../../rook/plugins/llm-cli/docs/manual.md): model providers.
- [security](../../rook/plugins/security/docs/manual.md) and [security-ml](../../rook/plugins/security-ml/docs/manual.md): the gates ([Security plugins](security.md)).
- [jobs](../../rook/plugins/jobs/docs/manual.md): scheduled, event, manual and webhook jobs ([Jobs](jobs.md)).
- [change](../../rook/plugins/changes/docs/manual.md): change records: what, why, risk, plan, scripts, tests and rollback; the hub runs standard changes and rolls them back, risky ones wait for one card. Plugin pushes need it.
- [backup-store](../../rook/plugins/backup-store/docs/manual.md): keeps the hub's sealed backups on another worker (backup target `worker:<name>`).

**Agents:**

- [agents](../../rook/plugins/agents/docs/manual.md): the agents runtime and the ready-made personas ([Agents](agents.md)).
- [agents-claude-code](../../rook/plugins/agents-claude-code/docs/manual.md): agent turns in the Claude Code CLI.
- [agents-codex](../../rook/plugins/agents-codex/docs/manual.md): agent turns in the Codex CLI.
- [claudecode](../../rook/plugins/claudecode/docs/manual.md): Claude Code rules written once on the hub.
- [decide](../../rook/plugins/decide/docs/manual.md): one-pass decision model for typed questions.
- [huddle](../../rook/plugins/huddle/docs/manual.md): group threads where people and agents in roles work something out.
- [github](../../rook/plugins/github/docs/manual.md): GitHub reads, and `github.merge`: how work lands (the task's sign-off for its trust level, then the gates, with the hub's token).

**Speech** (talk to your agents by voice; `docs/castling-speech.md` says what
runs where and what the phone app needs):

- [speech](../../rook/plugins/speech/docs/manual.md): voice sessions on the hub, a fast front talker, the agent behind it.
- [speech-engine](../../rook/plugins/speech-engine/docs/manual.md): the voice service's worker (speech recognition, voices, end of turn, speaker ID).
- [speech-elevenlabs](../../rook/plugins/speech-elevenlabs/docs/manual.md): spoken replies in ElevenLabs voices.

**Workbench** (on the hub; [The workbench](workbench.md)):

- [task](../../rook/plugins/tasks/docs/manual.md), [project](../../rook/plugins/projects/docs/manual.md), [knowledge](../../rook/plugins/knowledge/docs/manual.md), [concept](../../rook/plugins/concepts/docs/manual.md), [whiteboard](../../rook/plugins/whiteboard/docs/manual.md).

**Devices** (on workers):

- [shell](../../rook/plugins/shell/docs/manual.md): commands, environment, live terminals.
- [file](../../rook/plugins/file/docs/manual.md): read, write, list, search files.
- [browser](../../rook/plugins/browser/docs/manual.md): throwaway headless Chromium sessions.
- [screen-desktop](../../rook/plugins/screen-desktop/docs/manual.md), [hid-desktop](../../rook/plugins/hid-desktop/docs/manual.md), [notification-desktop](../../rook/plugins/notification-desktop/docs/manual.md): the desktop's screen, keyboard and mouse, notifications.
- [camera](../../rook/plugins/camera/docs/manual.md), [battery](../../rook/plugins/battery/docs/manual.md).
- [torrent](../../rook/plugins/torrent/docs/manual.md): Deluge.
- [dongle](../../rook/plugins/dongle/docs/manual.md): the Rook RP2040 USB dongle.
- Appliances ([what that means](workers.md#appliances)): [kvm](../../rook/plugins/kvm/docs/manual.md) (PiKVM), [tv](../../rook/plugins/tv/docs/manual.md) (LG webOS), [adb](../../rook/plugins/adb/docs/manual.md) (Android over adb), [cec](../../rook/plugins/cec/docs/manual.md) (Pico-CEC); [appliance](../../rook/plugins/appliance/docs/manual.md) is the delegate worker's side.

**Phone** (in the Rook Android app, which runs the 0.5 worker under
Chaquopy once Settings > Band > Rook version says 0.5; 0.4 stays the default,
and `docs/castling-phone.md` says what is still 0.4):

- Messages and people: [sms](../../rook/plugins/sms/docs/manual.md) (texts), [call](../../rook/plugins/call/docs/manual.md) (the call log, placing calls), [contact](../../rook/plugins/contact/docs/manual.md).
- Places and time: [location](../../rook/plugins/location/docs/manual.md), [clock](../../rook/plugins/clock/docs/manual.md) (timers and alarms), [calendar](../../rook/plugins/calendar/docs/manual.md).
- The screen and input: [screen-android](../../rook/plugins/screen-android/docs/manual.md), [ui-android](../../rook/plugins/ui-android/docs/manual.md) (apps by their labels), [hid-android](../../rook/plugins/hid-android/docs/manual.md), [app-android](../../rook/plugins/app-android/docs/manual.md), [clipboard-android](../../rook/plugins/clipboard-android/docs/manual.md).
- Sound: [sound-android](../../rook/plugins/sound-android/docs/manual.md) (volume, Do Not Disturb), [media-android](../../rook/plugins/media-android/docs/manual.md) (what is playing), [voice-android](../../rook/plugins/voice-android/docs/manual.md) (speak aloud, ask and hear the answer).
- The phone itself: [phone](../../rook/plugins/phone/docs/manual.md) (find, unlock, vibrate, flashlight), [battery-android](../../rook/plugins/battery-android/docs/manual.md), [notification-android](../../rook/plugins/notification-android/docs/manual.md) (notifications and decision cards).

## Writing your own

Most plugins are a page of YAML: [Writing a plugin](authoring.md).
