# Plugins

A plugin is a folder with a `plugin.yaml` (its **manifest**) and, if it
needs code, a few Python scripts. The manifest declares every cap the
plugin offers and what each one is (risk, scope, effects, arguments), so
the hub knows a plugin's caps before any worker runs it. Plugins don't
bundle the software they drive: the `torrent` plugin drives Deluge, it
doesn't ship it. Plugins only talk to each other through caps.

Some plugins run on workers (shell, files, devices), some on the hub (the
workbench, agents, notify), and a plugin can have parts on both.

A hub plugin can also serve a worker's caps: its scripts run on the hub
and reach the machine through the worker's primitives (`shell.exec`,
`file.*`, streams). An agent still calls `worker=kaiju cap=battery.status`
and finds it in kaiju's caps; nothing is installed on kaiju
([Writing a plugin](authoring.md#on-worker-caps)).

## The hub's library

The hub's **library** (`library:` in `hub.yaml`) is the folder of plugins
it trusts, one folder per plugin. At start the hub validates each one,
refuses any with errors (they are logged), and runs their hub parts. The
library is read again at every push, so a new version is just new files
there.

## Installing a plugin on a worker

Owners push a plugin from the library to one worker:

```sh
curl -s -H "$H" -d '{"worker":"kaiju","plugin":"browser","why":"Drive web pages on kaiju."}' \
  http://127.0.0.1:8740/plugins/push
```

What happens:

1. The hub validates the manifest, resolves the plugin's Python
   requirements for that worker (a conflict refuses the install with the
   reason instead of breaking the worker).
2. It signs the list of the plugin's file hashes **for that worker**
   (`plugin.sig`) and sends the bundle with the cap `core.plugin.install`,
   dispatched and journaled like any call; `core.plugin.rollback` is the way
   back. Its journaled why names the plugin and the version.
3. The worker installs it only when the signature is from a hub that may
   approve plugins there (its home hub or a co-owner) and every file
   matches. It loads the new version **without restarting** (nothing
   running on the machine is killed), keeps the previous version, and runs
   the plugin's `check` caps.
4. A failing check rolls the plugin back by itself. To go back by hand:

```sh
curl -s -H "$H" -d '{"worker":"kaiju","cap":"core.plugin.rollback","args":{"plugin":"browser"},"why":"The new version breaks browser.open."}' \
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
- [hub](../../rook/core2/hub/doors/web/builtin/hub/docs/manual.md): the hub and its workers (and which machines are sacred).
- [journal](../../rook/core2/hub/doors/web/builtin/journal/docs/manual.md): searching the journal.
- [llm](../../rook/core2/hub/doors/web/builtin/llm/docs/manual.md): which language models the hub can use.
- [page](../../rook/core2/hub/doors/web/builtin/page/docs/manual.md): the hub site's pages.
- [setting](../../rook/core2/hub/doors/web/builtin/setting/docs/manual.md): hub and per-person settings.
- [token](../../rook/core2/hub/doors/web/builtin/token/docs/manual.md): door tokens for agents (an agent mints its own MCP token).
- [backup](../../rook/core2/hub/backup/docs/manual.md): sealed backups of the hub that only the recovery key opens, and restoring one on a new machine.

**Core services** (install on the hub):

- [notify](../../rook/plugins/notify/docs/manual.md): reach a person on the channel that reaches them.
- [telegram](../../rook/plugins/telegram/docs/manual.md) and [discord](../../rook/plugins/discord/docs/manual.md): the band's bots.
- [llm-anthropic](../../rook/plugins/llm-anthropic/docs/manual.md), [llm-openai](../../rook/plugins/llm-openai/docs/manual.md), [llm-cli](../../rook/plugins/llm-cli/docs/manual.md): model providers.
- [job](../../rook/plugins/jobs/docs/manual.md): scheduled, event and manual jobs, each one call ([Jobs](jobs.md)).
- [backup-store](../../rook/plugins/backup-store/docs/manual.md): keeps the hub's sealed backups on another worker (backup target `worker:<name>`).

**Agents:**

- [agent](../../rook/plugins/agents/docs/manual.md): the agents runtime and the ready-made personas ([Agents](agents.md)).
- [agents-claude-code](../../rook/plugins/agents-claude-code/docs/manual.md): agent turns in the Claude Code CLI.
- [agents-codex](../../rook/plugins/agents-codex/docs/manual.md): agent turns in the Codex CLI.
- [agents-opencode](../../rook/plugins/agents-opencode/docs/manual.md): agent turns in OpenCode (`opencode run`), the first-run default with a free model.
- [attention](../../rook/plugins/attention/docs/manual.md): the house agent reads what tries to get your attention and keeps what matters in one place.
- [claudecode](../../rook/plugins/claudecode/docs/manual.md): Claude Code rules written once on the hub.
- [decide](../../rook/plugins/decide/docs/manual.md): one-pass decision model for typed questions.
- [huddle](../../rook/plugins/huddle/docs/manual.md): group threads where people and agents in roles work something out.
- [trust](../../rook/plugins/trust/docs/manual.md): the trust map: sacred machines, project levels, hard stops and what each agent may do.

**Speech** (talk to your agents by voice; `docs/castling-speech.md` says what
runs where and what the phone app needs):

- [speech](../../rook/plugins/speech/docs/manual.md): voice sessions on the hub, a fast front talker, the agent behind it.
- [speech-engine](../../rook/plugins/speech-engine/docs/manual.md): the voice service's worker (speech recognition, voices, end of turn, speaker ID).
- [speech-elevenlabs](../../rook/plugins/speech-elevenlabs/docs/manual.md): spoken replies in ElevenLabs voices.

**Workbench** (on the hub; [The workbench](workbench.md)):

- [task](../../rook/plugins/tasks/docs/manual.md), [project](../../rook/plugins/projects/docs/manual.md), [knowledge](../../rook/plugins/knowledge/docs/manual.md), [concept](../../rook/plugins/concepts/docs/manual.md), [whiteboard](../../rook/plugins/whiteboard/docs/manual.md).
- [change](../../rook/plugins/changes/docs/manual.md): change records: what, why, risk, testing, rollback and steps. Records only: they gate and run nothing.
- [github](../../rook/plugins/github/docs/manual.md): GitHub reads, `github.review` (sign off on a PR as yourself) and `github.merge` (lands it at the signed-off head once the task's sign-off meets its project's trust level).

**Devices** (on workers):

- [shell](../../rook/plugins/shell/docs/manual.md): commands, environment, live terminals.
- [file](../../rook/plugins/file/docs/manual.md): read, write, list, search files.
- [browser](../../rook/plugins/browser/docs/manual.md): throwaway headless Chromium sessions.
- [screen-desktop](../../rook/plugins/screen-desktop/docs/manual.md), [hid-desktop](../../rook/plugins/hid-desktop/docs/manual.md), [notification-desktop](../../rook/plugins/notification-desktop/docs/manual.md): the desktop's screen, keyboard and mouse, notifications.
- [camera](../../rook/plugins/camera/docs/manual.md), [battery](../../rook/plugins/battery/docs/manual.md).
- [torrent](../../rook/plugins/torrent/docs/manual.md): Deluge.
- [dongle](../../rook/plugins/dongle/docs/manual.md): the Rook RP2040 USB dongle.
- [dongle-s3](../../rook/plugins/dongle-s3/docs/manual.md): the Rook ESP32-S3 dongle, its own worker on the band (firmware; nothing runs on the hub).
- Appliances ([what that means](workers.md#appliances)): [kvm](../../rook/plugins/kvm/docs/manual.md) (PiKVM), [tv](../../rook/plugins/tv/docs/manual.md) (LG webOS), [tv-roku](../../rook/plugins/tv-roku/docs/manual.md) (Roku over ECP), [adb](../../rook/plugins/adb/docs/manual.md) (Android over adb), [cec](../../rook/plugins/cec/docs/manual.md) (Pico-CEC); the hub's [appliance](../../rook/core2/hub/appliance/docs/manual.md) registry onboards them and finds them on a worker's network (`appliance.discover`).

**Phone** (in the Rook Android app, which runs the worker under Chaquopy;
`docs/castling-phone.md` says how):

- Messages and people: [sms](../../rook/plugins/sms/docs/manual.md) (texts), [call](../../rook/plugins/call/docs/manual.md) (the call log, placing calls), [contact](../../rook/plugins/contact/docs/manual.md).
- Places and time: [location](../../rook/plugins/location/docs/manual.md), [clock](../../rook/plugins/clock/docs/manual.md) (timers and alarms), [calendar](../../rook/plugins/calendar/docs/manual.md).
- The screen and input: [screen-android](../../rook/plugins/screen-android/docs/manual.md), [ui-android](../../rook/plugins/ui-android/docs/manual.md) (apps by their labels), [hid-android](../../rook/plugins/hid-android/docs/manual.md), [app-android](../../rook/plugins/app-android/docs/manual.md), [clipboard-android](../../rook/plugins/clipboard-android/docs/manual.md).
- Sound: [sound-android](../../rook/plugins/sound-android/docs/manual.md) (volume, Do Not Disturb), [media-android](../../rook/plugins/media-android/docs/manual.md) (what is playing), [voice-android](../../rook/plugins/voice-android/docs/manual.md) (speak aloud, ask and hear the answer).
- The phone itself: [phone](../../rook/plugins/phone/docs/manual.md) (find, unlock, vibrate, flashlight), [battery-android](../../rook/plugins/battery-android/docs/manual.md), [notification-android](../../rook/plugins/notification-android/docs/manual.md) (notifications and decision cards).

## Writing your own

How to write one, with two worked examples: the agents' skill
[plugins](../../rook/plugins/agents/skills/plugins.md). Every manifest key:
[Writing a plugin](authoring.md).
