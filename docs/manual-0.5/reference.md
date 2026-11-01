# Reference

## hub.yaml

Paths are relative to the file. An unknown key is an error.

| Key | What |
|---|---|
| `data` | the data folder: keys, journal, records, tokens, settings, workers. Required |
| `band` | the band id (32 hex); else taken from the first `transport` band |
| `library` | the folder of plugins the hub trusts, one folder per plugin ([Plugins](plugins.md)) |
| `users` | people: `name: {preset, grants?, created?}`; at least one owner ([People](people.md)) |
| `agents` | agents and their rights: `name: {grants, max_risk?, effects?}` |
| `presets` | changes to the four presets for this hub, or presets of your own |
| `settings` | hub-side plugin settings: `plugin: {setting: value}` ([Settings](settings.md)) |
| `contracts` | which provider answers a shared cap: `llm.complete: llm-cli.complete` |
| `sacred` | worker names where every change is a hard stop ([Workers](workers.md#sacred-machines)) |
| `doors` | `http: {bind, web}` (`web: false` turns the site off, `{secure_cookie: true}` behind TLS); `mcp: {bind, path, origins, oauth}` (`oauth`: the door's public https address, for [claude.ai's connectors](mcp.md#claudeais-connectors)) ([MCP](mcp.md)) |
| `transport` | the band link: `bands: [{psk_env \| psk, relay \| ws_url}]` |
| `enroll` | what enrolling devices are told: `url`, `relay`, `ws_url`, `discover`, `services` ([Adding machines](enrollment.md)) |
| `log` | `{level, format: text \| json}` |

`init` writes a working file; a full example is at the top of
`rook/core2/hub/__main__.py`.

## worker.yaml

| Key | What |
|---|---|
| `data` | the worker's data folder: id, replay cache, memberships, bands, installed plugins. Required |
| `anchor` | the home hub, written at enrollment. Required |
| `name` | this machine's name on the band (default: the hostname; `rook` is the hub's) |
| `worker_id` | set at enrollment |
| `memberships` | other hubs' signed memberships (default `<data>/memberships.json`) |
| `plugins` | plugin folders, or folders of them, loaded at start |
| `bands` | extra bands by hand: `[{psk_env \| psk, relay \| ws_url}]` |
| `settings` | worker-side plugin settings: `plugin: {setting: value}` |
| `hardware` | what this machine has, for `requires.hardware` (`gpu`, `display`, `camera`, ...) |
| `description` | what the machine is for, up to 280 characters, shown to people and agents |
| `log` | `{level, format: text \| json}` |
| `run_as` | a worker running as root (Linux): the account its normal calls run as. Without it a root worker refuses to run anything; `root` runs them as root, as 0.4 did |

## Commands

| Command | What |
|---|---|
| `python -m rook.core2.hub --config hub.yaml init --owner <name> [--relay host:port \| --ws-url url] [--url https://…] [--bind addr]` | set up a new hub; sets the owner's sign-in password, prints an API token and the recovery key once |
| `python -m rook.core2.hub --config hub.yaml passwd <name>` | set or reset a person's sign-in password (typed, or one line on stdin); works at once |
| `python -m rook.core2.hub --config hub.yaml oauth approve <CODE> [--person NAME]` | approve the code a connector's sign-in page shows (claude.ai adding Rook); the page goes on within seconds ([MCP](mcp.md#claudeais-connectors)) |
| `python -m rook.core2.hub --config hub.yaml` | run the hub (SIGINT or SIGTERM stops it) |
| `python -m rook.core2.hub --config hub.yaml token --person <name> [--agent <name>] [--label text]` | mint a door token |
| `python -m rook.core2.hub --config hub.yaml recovery-key` | show the owner's recovery key |
| `python -m rook.core2.hub --config hub.yaml secret list \| set NAME [--description text] \| delete NAME` | the hub's vault; `set` reads the value from stdin ([Settings](settings.md#secrets)) |
| `python -m rook.core2.hub --config hub.yaml recover-vault` | open a `vault.db` from a hub that died with the recovery key (typed, or on stdin) |
| `python -m rook.core2.hub --config hub.yaml backup [run \| list \| verify [NAME]] [--target dir:/path \| rclone:remote:path]` | make, list or check sealed backups without the hub running ([Backup](../../rook/core2/hub/backup/docs/manual.md)) |
| `python -m rook.core2.hub --config hub.yaml restore (FILE \| --target T [--name NAME])` | rebuild a hub on a new machine from a backup; asks for the recovery key, writes this (new) config with `data/` and `library/` beside it |
| `python -m rook.core2.worker --config worker.yaml enroll <hub \| qr text> [--name] [--code] [--discover] [--insecure] [--force]` | sign this machine in with its home hub |
| `python -m rook.core2.worker --config worker.yaml` | run the worker |
| `python -m rook.core2.worker --config worker.yaml check` | import this core and check the config and core's manifests (what a core update runs before it switches; [Workers](workers.md#keeping-workers-up-to-date)) |
| `python -m rook.kit.manifest validate <folder>` / `diff <old> <new>` | check a plugin ([Writing a plugin](authoring.md#checking-and-trying-it)) |
| `python -m rook.core2.hub.doors.mcp_stdio [--url] [--token-file]` | the MCP door over stdio, for agents that need it |

## The HTTP door

Every request but `GET /health` carries `Authorization: Bearer <token>`.
Replies are JSON with `ok`; a refused call is still status 200, and the body
says why.

| Route | Body or query | What |
|---|---|---|
| `POST /call` | `{worker, cap, args?, why, session?, appliance?, elevate?}` | run a cap ([Caps](caps.md)) |
| `POST /session` | `{caps, project?, task?}` | start a session |
| `POST /session/end` | `{session}` | end one |
| `GET /workers` | | every worker, online or not |
| `GET /caps` | `?worker=` | what each worker serves |
| `GET /journal` | `?call=&cap=&worker=&person=&agent=&project=&kind=&session=&chain=&since=&until=&limit=` | journal entries ([The journal](journal.md)) |
| `GET /holds` | | calls waiting for a person |
| `POST /holds/<hold>` | `{decision: approve \| refuse}` | answer one ([Holds](holds.md)) |
| `POST /plugins/push` | `{worker, plugin, why}` | owners: install a library plugin on a worker, through a change record |
| `POST /doors/<name>` | `{open: true \| false}` | owners: the kill switch ([Security plugins](security.md#the-kill-switch)) |
| `POST /enroll/code` | `{}` | a one-time code and its QR text |
| `POST /enroll/claim` | `{code, name}` | no token: a device claims its config, once |
| `POST /enroll/device` | `{name}` | no token: a device code to poll and a code to show |
| `POST /enroll/poll` | `{device_code}` | no token: pending, refused, or the config, once |
| `GET /enroll/pending` | | devices waiting for approval |
| `POST /enroll/approve` | `{code, decision?}` | approve or refuse a device's code |
| `GET /recovery-key` | | owners: the recovery key |
| `POST /bands/invite` | `{role?, ceiling?}` | owners: an invite to this hub's band |
| `POST /memberships` | `{worker, why, invite? \| band?, role?, ceiling?}` | owners: a worker's other bands |
| `GET /health` | | no token: the hub answers |
| `GET /updates/desktop.json` | | no token: the desktop app's files in Rook's latest release, for its update check |

The hub site is on `/`, `/login` and `/web/` ([The hub site](dashboard.md)).
A hub plugin's own endpoints (its manifest's `routes:`) are under
`/p/<plugin>/<path>`: `auth: user` takes the bearer token, `auth: none`
lets anyone in and the plugin checks. `auth: {hmac: "{{settings.x}}"}` takes
a request whose `X-Hub-Signature-256` is the HMAC-SHA256 of its body under
that secret (GitHub's form, `sha256=<hex>`), `auth: {token: "{{settings.x}}"}`
one whose `Authorization: Bearer` is the token; `header:` names another
header (Telegram sends `X-Telegram-Bot-Api-Secret-Token`). Anything else is
401, and so is every request while the secret isn't set.
