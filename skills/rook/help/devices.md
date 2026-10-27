---
title: Adding a device by asking
pitch: Put a lock, light, plug or sensor on the band from its local API: find it, define its caps on a delegate, test, confirm
tools: []
caps: [devkit.]
refs: [references/tools.md]
---
# Adding a device by asking

**Use it when** the person says "add my front door lock", "put the garage door on Rook", "can you control the porch light?". You build the device yourself on a delegate worker (the `devkit` plugin): no integration to install.

## Steps

1. **What and where.** Make and model, and its address. Ask the person only what you can't find. `devkit.discover` on a worker on that network lists what answers SSDP/UPnP and mDNS (server strings, model names, service types like `_hap._tcp`, ports). `devkit.list` on a delegate shows what is already defined.
2. **Its local API.** The maker's developer docs, the web, the device's own status page. Prefer a documented local API (HTTP/JSON, a TCP line protocol) to a cloud one. No local API at all: say so and stop; don't guess at undocumented writes to a lock.
3. **The delegate.** An always-on worker on the same network (`rook_workers`; not a laptop that sleeps). It must have the `devkit` plugin.
4. **Credentials.** Never ask for a password in chat or by voice. Ask the person to store it under **Trust > Secrets** with a name you give (`front-door.token`). Then grant that name to the delegate: `rook_call(worker="rook", cap="settings.set", args={"key": "devkit.secrets", "value": ["front-door.token"], "scope": "worker", "target": "<delegate>"})`. The value is a list of vault **names** (add to what is there: `settings.get` first). It is an admin change: the trust map may ask the person.
5. **Draft and test.** Write the definition (below) and run each read cap with `devkit.test(definition=…, cap="state")` until the reply and `extract` are right. A cap that uses a `{secret:…}` can't be tested from a draft (the draft's host would be yours to pick): define the device first, then `devkit.test(device="front-door", cap="state")`. **Testing an action does it**: test `lock` or `turn_on` only with the person's go-ahead, and never test `unlock`/`open` unless they ask you to, standing by.
6. **Define and confirm.** `devkit.define(definition=…)`. The reply has the device's `worker_id`, caps with their risk, and `secrets_ready`. Check it on the band (`rook_workers(name="front-door")`), call its state cap on the device itself (`rook_call(worker="front-door", cap="lock.state")`), and tell the person what they can now ask for.

## The definition

```json
{"name": "front-door", "type": "lock", "model": "Acme L2",
 "vars": {"host": "192.0.2.20"},
 "caps": {
   "state":  {"doc": "Locked or not, and the battery.",
              "http": {"url": "http://{host}/api/state",
                       "headers": {"Authorization": "Bearer {secret:front-door.token}"},
                       "extract": {"locked": "state.locked", "battery": "state.battery"}}},
   "lock":   {"doc": "Lock the door.",
              "http": {"method": "POST", "url": "http://{host}/api/lock",
                       "headers": {"Authorization": "Bearer {secret:front-door.token}"},
                       "body": {"locked": true}}},
   "unlock": {"doc": "Unlock the door.",
              "http": {"method": "POST", "url": "http://{host}/api/lock",
                       "headers": {"Authorization": "Bearer {secret:front-door.token}"},
                       "body": {"locked": false}}}},
 "state": {"cap": "state", "every": 60}}
```

- Credentials: `{secret:name}` with **single** braces. `{{secret:…}}` would be filled in by the hub on the way and stored on the delegate, so a definition with it is refused, as is a literal token or password.
- `name` is what the band shows; `type` is what it is and the caps' prefix (`lock.unlock`). Pick a name no worker already has.
- Recipes: `http` (method, url, headers, query, body, `auth: {type: basic|digest, user, password}`, `insecure` for a self-signed device certificate, `ok`: statuses that count), `tcp` / `udp` (host, port, `send` or `send_hex`, `read` / `reply`), `argv` (a local program, no shell; secrets only in `env`). One per cap.
- `args`: `{"level": {"type": "int", "min": 0, "max": 100}}`, used as `{level}`; a body string that is exactly `"{level}"` sends the number. `vars` are the definition's constants (`{host}`).
- `extract`: `"state.locked"`, `{"name": "path", …}`, `"re:temp=([0-9.]+)"`, `"$status"`.
- `state`: a read cap with no required args, checked every `every` seconds (15-3600); its reading is the device's heartbeat.

## Risk, honestly

- Name every cap for what it does. A GET is a read only when its name says it reads (`state`, `status`, `get_…`, `…_level`, `battery`, `info`…) and has no action verb; anything else (`open_garage`, `relay`, `turn_on`, `set_level`) is at least `write`, because many plugs and relays (Shelly, Tasmota) switch on a plain GET.
- The host and port come from `vars` only; a cap's args can't pick where it goes.
- Reads are `read` (only GET/HEAD can be). Everyday reversible actions (a light, a volume) stay `write`. A device of type `lock`, `alarm`, `valve`, `garage`, `gate` or `door` is a way into the house: every action on it is `admin` by itself (as Home Assistant locks and garage doors are). Elsewhere, an action that matters if it happens at the wrong time (turn off a freezer, start a heater) is `"risk": "exec"`; add `"tags": ["destructive"]` if it can't be undone, `["sensitive"]` for a camera snapshot. The plugin never lets a cap go below its recipe's floor and tags every action `physical`; the trust map and approvals do the rest. Don't try to route around them.

## Changing or removing

- Define again with the same `key` (default: the name lowercased) to change it; the worker id stays. `devkit.remove(device=…)` takes it off the band. Both keep the old definition in the delegate's `retired.json`.
