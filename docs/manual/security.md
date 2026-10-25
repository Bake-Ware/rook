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
- **Risky agent calls wait for you.** When an agent wants to do something
  admin-level or destructive (update or restart a machine, push config, change
  secrets or settings), or starts reaching more than 5 machines in 10 minutes,
  the call is held until you approve it on the **Approvals** page, where it
  reads as a plain ask: a title, what it is for, who asks, what will happen
  and the risk, with the technical detail under Explain. A
  prompt-injected agent cannot run one bad command on every machine at once.
  People are never held. See [[manual-accounts-and-access]].
- **Permissions and guardrails** limit what agents and jobs may do
  ([[manual-accounts-and-access]], [[manual-jobs]]).
- **Secrets** keep passwords out of agents' sight and out of logs
  ([[manual-secrets]]).
- **Password guessing is slowed down.** After a few wrong passwords or tokens
  from one address, the hub refuses that address for a while ("Too many
  attempts, try again in N seconds"), longer each time. A hub you reach from
  the internet should still sit behind HTTPS (a reverse proxy or tunnel).

## Advanced security

Most agent calls just go, and the journal records them. Only the hard stops
ask you: a change on a sacred machine, erasing data for good, revealing a
secret, and sending a message as you ([[manual-trust]]). The **advanced
security** plugin (Manage > Plugins, off by default) brings back the older,
stricter holds: admin and destructive calls, workers on the hub's own
machine, more than 5 machines in 10 minutes, guarded trust entries, relaying
approvals through agent clients, and votes before merges.

## What Rook cannot do yet

- A machine that already knows the band key can keep using it until it is
  switched off. Removing or revoking only works on machines that cooperate.
  Per-machine identity that can shut out a hostile machine is planned.
- Anyone who can run commands on a worker has the rights of the user the worker
  runs as. Install workers as an ordinary user where you can.
- Approval holds work at the hub. Calls one worker sends straight to another
  do not pass the hub and are not held.
- A worker on the hub machine itself can change the hub's files, approvals
  included. Rook holds commands to such a worker by default, but it needs to
  recognise the machine; if it can't (the Approvals page says so), name those
  workers under Settings. A person with a login on the hub box is trusted
  either way.
- A dashboard or account password that you also keep as a band secret can't be
  used to approve held calls, because an agent could sign in with it. The
  Approvals page warns you and disables the buttons.
- Approving by voice is weaker than the dashboard: anyone near the phone can
  answer. It is off unless you choose it.
- A client you let relay your approvals is trusted to pass on your words
  faithfully; Rook records what it says you said but can't check it (the
  chair is the exception: it can only quote your own message). Turn it on
  only for clients you trust, and keep the dashboard-only list strict.
  Secrets, tokens, policy, settings and job locks are never relayed.
