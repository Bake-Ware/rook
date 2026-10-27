---
id: secrets
title: Secrets
order: 12
features:
  - features/secrets
since: 0.1.0
deprecated: false
---
# Secrets

**Secrets** is a locked box on the hub for the passwords and keys your agents
and jobs need. It is only for the operator.

## Why use it

Agents never need to see a password. They write a placeholder such as
`{{secret:nas-password}}` where the password goes; the hub fills in the real
value on its way to the machine, and hides it again in the answer and in every
log. Every time a secret is used, Rook records who used it, how and for which
task.

## Adding a secret

1. Open **Secrets** (under Trust).
2. Give the secret a short name, such as `nas-password`, and paste its value.
3. Save. The value is write-only: the page never shows it again.

## Automatic hiding

If a stored secret turns up anywhere Rook shows or keeps (a command's output, a
file, a chat message, a knowledge page, a terminal), Rook replaces it with its
placeholder, whoever put it there. That includes common encodings of the value.
Values shorter than 8 characters are not hidden this way, because they would
match ordinary words; use longer secrets.

What it cannot catch: a secret hidden inside a larger encoded blob, and traffic
between two machines that never passes through the hub.

## Reading a secret directly

An agent can read a value outright with `rook_secret` when it really must. That
read is logged like every other use.
