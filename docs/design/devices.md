# Device workers: things a delegate answers for

Status: implemented (worker framework, hub roster, dashboard, `webos` driver).
Wire format: core spec 14.6. User manual: `docs/manual/devices.md`.

## 1. Goals

- Put things that can't run a worker (a TV, a light switch, a thermostat) on
  the band as **workers of their own**, so agents address them by name, see
  their live state in the roster, and policy, approvals and the journal treat
  them like any worker.
- Keep the worker that drives them (the **delegate**) an ordinary worker: no
  new transport, no second connection, no hub plugin.
- Make adding a new kind of device a plugin's job: a driver declares devices
  with `@capability` methods, the same way plugins declare caps.

Non-goals for now: devices that run their own (dormant) worker, a separate
credential per device, and drivers other than LG webOS. Section 6 says how
each fits later.

## 2. Model

- `rook.core.devices.Device`: one device. `NAMESPACE` (caps namespace, e.g.
  `tv`), `TYPE` (`tv`, `light`, `climate`…), `DRIVER` (`webos`…), a stable
  `key` and a `name`. Caps are `@capability` methods; `heartbeat()` returns
  its compact live state; `info()` the announce `device` block.
- A worker plugin becomes a delegate by implementing `devices() -> [Device]`.
  The plugin owns the devices' lifecycle (connections, polling, background
  tasks) in its own `start`/`stop`, so enabling, disabling or reloading the
  plugin brings its devices and takes them away.
- `rook.worker.devices.DeviceTable` (on the `Worker`) rebuilds from the loaded
  plugins on every announce. Each device gets its own `CapabilityRegistry`:
  its caps, plus `caps.describe` (the hub fetches it for hash-only announces
  and the dashboard's call forms) and `info.ping`.
- A device's `worker_id` is derived: `"d"` + 31 hex of
  `SHA-256("device\0<delegate id>\0<plugin namespace>\0<key>")`. Stable across
  delegate restarts; 32 hex characters like a uuid4 id, so nothing that stores
  or validates worker ids needs to change.

## 3. Wire

All additive (core spec 14.6):

- The delegate sends its own announce with `devices: [<device ids>]`, then one
  announce per device: the usual keys, `features: []`, `plugins: []`, the
  delegate's `version`/`build`/`authz`, the device's `tiers` and `hb` (under
  its namespace), plus `delegate: <delegate id>` and `device: {type, driver,
  model?}`. The key is `delegate`, not `via`: `via` already means the
  on-behalf-of principal chain in tickets and journal rows.
- A device declares no route. A caller sends it a CHANNEL request targeted at
  the device id; the hub addresses it to the delegate's route when the
  delegate declares one (`BandClient._delegate_route`), so only the delegate
  receives it, and otherwise broadcasts as for any node without `route`.
- The delegate dispatches a request whose `target` is one of its devices into
  that device's registry and replies with `from` = the device id. Open calls
  are the delegate's own. Tickets are checked against the device id
  (`Guard.check(target=…)`) with the delegate's keys and replay cache.

## 4. Hub

- `BandClient._handle_announce` keeps a node's claim (`delegate_claim`,
  `device_info`) apart from the fields everything else reads. `_link_device`
  sets `delegate`/`device` only while the claimed delegate is a live
  non-device worker whose own announce lists the node, and `rook.core.devices.is_device`
  reads that. So a worker can't make itself a "device" (undeauthable, never
  updated) just by claiming a delegate; until linked it is an ordinary worker.
  `device` is `clean_device`: at most 8 short scalars, `type` always set.
- A delegate's announce drops any linked device it no longer lists
  (`_prune_devices`), and the GC drops a delegate's devices when it evicts the
  delegate.
- Roster: `rook_workers` rows carry `delegate` (the delegate's name) and
  `device`, empty (so omitted) on ordinary workers; the legacy `fields=all`
  shape is unchanged for them. `rook_caps` doesn't count devices toward `"*"`
  / `all_but`.
- Worker-only operations skip devices: updates (`member_kind` → `device`,
  left out of members, rollout and floors), the auto-push loop, deauth (a
  device is refused with a pointer to its delegate; a banned delegate's
  devices count as banned), ccmod's roster and presence events, hygiene's
  host lookup, the settings pages' worker lists and relay peer count, `hub.info`'s
  worker count, and the `cap://any/…` resolver (devices only when no
  ordinary worker has the cap).
- Tier floor: `authz.declared_floor` reads Device classes too, so a device
  announce can't lower `tv.pair` below admin.

## 5. Dashboard

The worker list shows a device with a type icon (TV, generic device), a
**via <delegate>** tag, its driver in place of the version, and its status
(`on · YouTube · vol 12` / `off` / `not paired`) in the battery slot, built
from `hb[device.type]`. The 3D view draws devices as chips. `/api/band/workers`
rows carry `delegate`, `delegate_name` and `device`.

## 6. Later

- **Dormant devices that run their own worker** (e.g. a JS worker inside a
  TV app): the device worker takes over the same derived id, the delegate
  becomes its waker and fallback. The roster already has the shape for it
  (`delegate` names the waker).
- **Per-device credentials**: a delegate is trusted for the devices it
  lists, like `facts`; anyone with the band PSK can run a delegate. When
  per-device identities land, the hub can accept `devices` only from workers
  allowed to delegate.
- **More drivers**: Home Assistant (one device per HA *device*, not per
  entity: a thermostat is one worker with a few caps), HDMI-CEC, PiKVM.
