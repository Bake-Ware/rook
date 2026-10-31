# Secrets

The hub keeps secrets in its vault, `<data>/vault.db`. Each value is sealed
under the vault's data key, and that key is kept sealed twice: under a key
derived from the hub's root key (the hub opens the vault when it starts) and
under the owner's recovery key (the vault opens again on a new hub).

Only owners use the vault, in person or through their agents.

## Using a secret

Put `{{secret:NAME}}` in a call's args, alone or inside a string
(`"Authorization: Bearer {{secret:gh-token}}"`). The hub puts the value in
only for the call it sends to the worker that runs it. The journal keeps the
reference, and the value is replaced by `[redacted]` wherever it would come
back in the call's error or result. Calls to the hub's own caps keep the
reference as text, so a page or a task never stores a value.

A hub plugin's secret setting holds a vault name; the plugin reads the value
when it needs it.

## Caps

| Cap | What it does |
|---|---|
| `secret.list` | names, descriptions, times; never a value |
| `secret.set` | store or replace one (the value is journaled as `[redacted]`) |
| `secret.delete` | erase one; the person answers first |
| `secret.reveal` | show a value; the person answers first, every time |

## From the hub's machine

    python -m rook.core2.hub --config hub.yaml secret list
    python -m rook.core2.hub --config hub.yaml secret set NAME [--description TEXT]   # the value on stdin
    python -m rook.core2.hub --config hub.yaml secret delete NAME

## When the hub dies

Copy `vault.db` into the new hub's data folder, then:

    python -m rook.core2.hub --config hub.yaml recover-vault      # type the recovery key

The vault is sealed under the new hub's root key, and your recovery key
becomes this hub's recovery key. Until then, calls that use a secret fail
with "the vault is locked".
