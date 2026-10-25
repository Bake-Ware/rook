---
id: security
title: Keeping your machines safe
order: 22
features:
  - features/security
  - plugin/hub/security
  - settings/security
since: 0.1.0
deprecated: false
---
# Keeping your machines safe

Rook can run commands on all of your machines, so a few habits matter.

## The band key is the master key

Anyone who knows a band's key can join that band and control its machines.

- Never share a band key in chat, email or a screenshot. Use pairing codes to
  add machines ([[manual-adding-machines]]).
- Keep backups of the hub's data folder private: it holds the keys.
- If a device with the key is lost or stolen, change the band key and re-pair
  every machine you still trust ([[manual-bands-and-keys]]).

## What protects you

- **Encryption.** Everything inside a band is encrypted. The hub passes the
  messages along without being able to read them.
- **Signed control.** Updating a machine or removing it from the band needs
  your hub's signature, so another machine on the band cannot do it.
- **Removing a machine** (deauth) parks it off the band, even after a reboot,
  and the hub ignores it from then on.
- **Tokens per agent.** Each agent has its own token, every call is recorded
  under its name, and you can revoke a token at any time
  ([[manual-agents-and-mcp]]).
- **A few things always ask you.** Before an agent changes a sacred
  machine, erases data for good, reveals a secret or sends a message as
  you, the hub asks you first. Everything else goes and is recorded in the
  journal. People are never held. See [[manual-trust]].
- **Permissions and guardrails** limit what agents and jobs may do
  ([[manual-accounts-and-access]], [[manual-jobs]]).
- **Secrets** keep passwords out of agents' sight and out of logs
  ([[manual-secrets]]).
- **Password guessing is slowed down.** After a few wrong passwords or tokens
  from one address, the hub refuses that address for a while ("Too many
  attempts, try again in N seconds"), longer each time. A hub you reach from
  the internet should still sit behind HTTPS (a reverse proxy or tunnel).

## Advanced security

The **advanced security** plugin (Manage > Plugins, off by default) brings
back the stricter rules Rook used to have:

- every admin or destructive agent call waits for you, and so do your own
  hold rules in the policy;
- commands to a worker on the hub's own machine wait for you;
- an agent reaching more than 5 machines in 10 minutes waits for you;
- guarded trust entries ask first, and so do admin actions on anything not
  on the map;
- the house agent also asks about anything not easily undone and anything
  on a family device;
- only agent clients you tick may pass on your answers, and the huddle
  chair too ([[manual-accounts-and-access]]);
- every new hold sends you a message (Telegram, a notification or your
  phone's voice);
- merges, deploys and ships need a blind vote of the roles
  ([[manual-conference-table]]).

Agents can't switch it on or off; only you can.

## What Rook cannot do yet

- A machine that already knows the band key can keep using it until it is
  switched off. Removing or revoking only works on machines that cooperate.
  Per-machine identity that can shut out a hostile machine is planned.
- Anyone who can run commands on a worker has the rights of the user the worker
  runs as. Install workers as an ordinary user where you can.
- The checks on commands look for known patterns. They catch mistakes, but
  they are not a sandbox: a command they don't recognise still runs (a sacred
  machine reached by IP address or by an ssh alias Rook doesn't know, a
  script that deletes files its own way), and the journal shows it.
- The hub checks calls that pass through it. Calls one worker sends straight
  to another do not pass the hub and are not checked.
- A dashboard or account password that you also keep as a band secret can't be
  used to approve held calls, because an agent could sign in with it. The
  Approvals page warns you and disables the buttons.
- Approving by voice is weaker than the dashboard: a voiceprint is not a
  password. It counts only when the voice is sure it is you, and it is off
  until you turn it on ([[manual-phone-attention]]).
- A worker on the hub machine itself can change the hub's files. Commands
  that name the vault or guardrail files ask you, but that is a pattern
  check. With advanced security on, every command to such a worker waits
  for you, if Rook recognises the machine (if it can't, the Approvals page
  says so; name those workers under Settings). A person with a login on the
  hub box is trusted either way.
- A client that passes on your answer is trusted to quote you faithfully.
  Rook records what it says you said but can't check it (the chair is the
  exception: it can only quote your own message). Hard stops, secrets,
  tokens, policy, settings and job locks are never passed on.
