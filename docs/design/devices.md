# Device workers: things a delegate answers for

Status: implemented (worker framework, hub roster, dashboard, `webos` and
`homeassistant` drivers).
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

Non-goals for now: devices that run their own (dormant) worker and a
separate credential per device. Section 8 says how each fits later.

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
from `hb[device.type]` (else the device's only `hb` entry). A driver whose
devices vary (Home Assistant) puts a ready summary in `hb.<ns>.text`, which
the dashboard shows as is. The 3D view draws devices as chips. `/api/band/workers`
rows carry `delegate`, `delegate_name` and `device`.

## 6. webOS driver notes (round two, 2026-10-27)

- **Wake-on-LAN goes to every MAC the TV has.** A TV's `getinfo` reports its
  wired and its Wi-Fi MAC whichever it uses, and it only wakes on the one it
  is connected by. The first version kept the wired one, so `tv.power_on`
  never woke the house TVs (both on Wi-Fi; checked live: the wired MAC did
  nothing in 15 s, the Wi-Fi one woke the living-room TV in 4 s). The driver
  now keeps every MAC (`macs.json` holds a list; a string from the first
  version still loads), puts the one the delegate's ARP table shows first,
  and also reads the ARP table at `tv.power_on`, so a TV last seen on before
  the upgrade wakes too.
- **`tv.power_on` waits** (default 20 s, `wait=0` for fire-and-forget),
  resending every 2 s, and fails with the reason, including the TV's own
  "Turn on via Wi-Fi" setting (`settings/getSystemSettings` network
  `wolwowlOnOff`, read once per connection; `tv.status` `wake_on_lan`).
- **A request on a connection that died under it is retried once** on a
  fresh one; a request that times out drops the connection so the next call
  reconnects instead of waiting on a half-dead socket. `tv.power_off` treats
  the TV dropping the connection as success and never sends twice.
- **App names as people say them**: case, spaces and punctuation ignored,
  `+` read as "plus"; same name, then prefix, then substring; a miss re-reads
  the app list once; an input name gets "use tv.input".
- **New caps**: `tv.type` (IME `insertText`/`sendEnterKey` into the open
  field; the reply names the app in front, and `app=` refuses to type into
  any other) and `tv.sleep` (a wall-clock timer kept in `sleep.json`, ticked
  every 15 s; one missed by more than 10 min while the delegate was down is
  dropped, not acted on late; shown as `hb.tv.sleep_at`; cleared when the TV
  is switched off another way).
- `tv.power_on` asks the TV for its power state before answering "already
  on": a connection that died without closing still looks on. `tv.status` adds `sound_output` (`tv.volume`
  notes when sound goes to an external device) and `wake_on_lan`.

## 7. Home Assistant driver

- **One device worker per HA device**, chosen in `homeassistant.devices`
  (HA name or id, optionally `band-name=`). Opt-in, not "everything": a real
  house's HA has dozens of devices, many duplicates and dead ones (the one
  this was built against: dozens, half of them unavailable, several
  sharing one name), and names that would collide with other band workers
  (an HA "Office TV" next to the webOS `office-tv`). `homeassistant.devices`
  lists the candidates, working ones first, to pick from.
- **Kind** (`device.type`): from the first of climate, light, fan, cover,
  lock, vacuum, media player, humidifier, water heater, camera, alarm, valve
  among its main (uncategorised) entities; `switch` only when all it has are
  switches and power meters (a plug or strip); else `device` (a fridge whose
  only main controls are ice-maker switches).
- **Caps are the same on every device**, small and generic: `ha.status`,
  `ha.on`/`ha.off` (lights take brightness, colour, kelvin), `ha.set` (one
  value, meaning by domain), and `ha.service` for anything else. With no
  `entity`, on/off/set act only when there is exactly one candidate; never a
  guess across a power strip's sockets. Sirens and covers (blinds) switch only
  when named. Locks, alarm panels, valves (a water main) and covers that are
  a way in (device class garage, gate or door) only through `ha.service`,
  which is `admin` because it can unlock a door; the rest are `write`,
  physical. `homeassistant.scene` is `admin` too: a scene can do anything.
  `ha.service` always targets the device's own entities (a `data` naming a
  target is refused).
- **Renames**: each configured entry is bound to its HA device id at the
  first match (`bindings.json`), so renaming the device in HA neither drops
  it nor changes its band name (trust entries and jobs keep working).
  Entries no single HA device matches are listed as `not_found` by
  `homeassistant.devices`.
- **Live state**: one WebSocket to HA; registries and states read once per
  connection, then `state_changed` events; registry events re-read the
  registries (debounced). The configured devices' registry entries are saved
  (`devices.json`), so after a restart with HA down they are listed as
  unavailable rather than missing.
- **Scenes** belong to no device, so they are delegate caps:
  `homeassistant.scenes` and `homeassistant.scene`.
- Tested live (read-only, plus one no-op service call) against the house's
  HA 2026.7.2; unit-tested against a fake HA WebSocket API with its real
  message shapes.

## 8. Later

- **Dormant devices that run their own worker** (e.g. a JS worker inside a
  TV app): the device worker takes over the same derived id, the delegate
  becomes its waker and fallback. The roster already has the shape for it
  (`delegate` names the waker).
- **Per-device credentials**: a delegate is trusted for the devices it
  lists, like `facts`; anyone with the band PSK can run a delegate. When
  per-device identities land, the hub can accept `devices` only from workers
  allowed to delegate.
- **More drivers**: HDMI-CEC and PiKVM as devices.
- **Devices by asking** (agent-written recipes, no driver): [devices-by-asking.md](devices-by-asking.md).
