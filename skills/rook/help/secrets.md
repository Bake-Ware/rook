---
title: Secrets and the vault
pitch: Use a credential without ever seeing or spreading its value
tools: [rook_secret]
caps: []
refs: [references/examples.md]
notes:
  - rook_secret: Prefer {{secret:name}} inside rook_call args: the hub substitutes and masks it. action=get reveals the raw value, is a hard stop and is logged with your task.
---
# Secrets and the vault

**Use it when** a call needs a password, token or key.

## How

- Put `{{secret:name}}` in `rook_call` args or `rook_console_write` text; the hub substitutes it and masks it in replies and the journal. `rook_secret(action="list")` gives the names.
- Masked stubs you get back (`{{secret:name|b64}}`) can be passed straight back in.

## Hygiene

- **Avoid `rook_secret(action="get")`**: it returns the raw value, asks the person first and is logged with your task.
- **Never paste a value** into chat, knowledge, handoffs, task attrs, a commit or a command line (`--psk` is visible in `ps`; use the environment).
- **If one leaks**, say which vault name so the user can rotate it.
- **The band key is root on every worker.** Never print it or put it anywhere.
