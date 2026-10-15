---
id: security
title: Keeping your machines safe
order: 22
features:
  - features/security
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
- **Permissions and guardrails** limit what agents and jobs may do
  ([[manual-accounts-and-access]], [[manual-jobs]]).
- **Secrets** keep passwords out of agents' sight and out of logs
  ([[manual-secrets]]).
- **Password guessing is slowed down.** After a few wrong passwords or tokens
  from one address, the hub refuses that address for a while ("Too many
  attempts, try again in N seconds"), longer each time. A hub you reach from
  the internet should still sit behind HTTPS (a reverse proxy or tunnel).

## What Rook cannot do yet

- A machine that already knows the band key can keep using it until it is
  switched off. Removing or revoking only works on machines that cooperate.
  Per-machine identity that can shut out a hostile machine is planned.
- Anyone who can run commands on a worker has the rights of the user the worker
  runs as. Install workers as an ordinary user where you can.
