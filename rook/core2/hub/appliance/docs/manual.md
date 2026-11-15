# Appliances (hub)

An appliance (a TV, a lamp, a PiKVM, a phone over adb) runs no worker. A
**delegate** worker that can reach it carries its calls, like a reverse proxy
through the hub. Agents call the appliance by its name, the way they call a
worker:

```
call worker=living-room-tv cap=tv.power.off
```

The hub looks up the instance and picks its known-good delegate; the
plugin's script runs on the hub with the instance's values in
`ctx.appliance` (address, port, its settings, secrets from the vault) and
reaches the device through that delegate (`ctx.worker`): its `shell.exec`,
or a network stream (`net.open`, `net.udp`).

## Onboarding: `appliance.add`

Find the device from a worker on its network (`appliance.discover`, a cap of
every worker that serves `net.udp`: an SSDP search and an mDNS browse from
that worker), then:

```yaml
name: living-room-tv
plugin: tv
worker: janitor
host: 192.168.1.40
pair: { timeout: 60 }
```

If the plugin declares a pair cap (`appliance.pair` in its manifest), it runs
first through that worker: pairing is the proof the worker reaches the
device, and the route is recorded only when it works. Settings the pair cap
returns become instance settings; secret ones go to the vault as
`appliance.<name>.<key>`. Secret settings given here are vault names
(`{{secret:pikvm-password}}` or `pikvm-password`), never values.

`appliance.add` again with the same name and plugin and another worker adds
that worker as a further delegate.

## Routes

The first delegate is the known-good one. A call goes to it when it is
online, else to the next online delegate (stored status is only a hint: with
none online the known-good one is still tried). When a send fails before it
reaches the delegate, or a retry-safe call times out, the next delegate is
tried in the same request. The delegate that answered becomes the known-good
one.

## Settings travel with the call

Secret plugin settings and `where: hub` settings of any worker plugin are kept
on the hub (`setting.set`), and an appliance's instance settings in this
registry. The hub sends them with each call, secrets resolved from the vault
for that call only: they are never written to the worker's disk and never
journaled.
