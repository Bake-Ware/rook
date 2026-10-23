---
id: settings
title: The Settings page
order: 15
features:
  - settings/core/general
  - settings/core/network
  - settings/core/storage
  - settings/core/updates
  - settings/core/worker-defaults
  - settings/core/features
  - settings/relay
  - plugin/hub/settings
since: 0.1.0
deprecated: false
---
# The Settings page

**Settings** (under Manage) holds every setting Rook has, for the hub, your
bands, each machine, each plugin and you personally, in one place.

## Reading a setting

Each row shows:

- the setting's name and what it does (click the row for more help);
- its current value;
- **Comes from**: where the value comes from. In order of strength: an
  environment variable or command-line option (locked; change it where the hub
  is started), a value saved on this page, an older setup file, or the
  built-in default;
- **Applies**: when a change takes effect. *live* means at once (on a hub
  with the dashboard and the MCP service as two processes, the other one picks
  it up within a few seconds); *reload* means the plugin restarts; *restart*
  means the hub or machine must restart.

Passwords and keys are write-only: the page shows that one is set, never its
value.

## Restarting the hub

**Restart** (top right, operator only) restarts the hub so *restart* settings
take effect. The page waits and reloads itself when the hub is back, usually
within a few seconds; machines and agents reconnect on their own.

It works when the hub runs as a systemd service that starts it again
(`Restart=on-failure` or `always`, as the installer and `rook hub deploy` set
up). Otherwise the page says why and you restart the hub the way you started
it. A hub kept running by something else, such as a Docker restart policy, can
set `ROOK_RESTART_SUPERVISED=1` to allow the button.

## Where settings live

- **Overview**: a summary, and any settings that need attention.
- **Hub**: the hub's own settings, grouped as General (its public address and
  band name), Network (addresses and ports), Sign-in, Storage (where its files
  are kept), Updates, Features, Plugins, and Keys & access.
- **Bands**: settings for one band.
- **Workers**: settings for one machine. Choose **Apply to this worker**; a
  risky change restarts the machine and is undone automatically if it does not
  come back.
- **Worker defaults**: what new machines start with (how often they check in,
  how they check for updates, their log level, permission checks).
- **Plugins**: one page per plugin, such as Jobs, Knowledge or Telegram.
- **My preferences**: your own settings.
- **Persona**: see [[manual-agents-and-mcp]].
- **Relay (display only)**: the relay's settings, for reference. Change them
  where the relay is started.

Every change is recorded with who made it and when (**History**). Agents and
scripts can read and change settings with the `settings.*` capabilities on the
hub; changing them needs operator rights.

The full list of settings, with their environment variables and defaults, is
in docs/operations/settings-reference.md.
