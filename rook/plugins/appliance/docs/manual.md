# Appliances (worker side)

An appliance (a TV, a lamp, a printer) runs no worker: a delegate worker on
its network carries its calls (spec 5.7). Its caps come from a plugin with an
`appliance:` section, usually a config plugin (YAML, no code). This plugin is
the worker's part in adding one.

## Find it: `appliance.discover`

SSDP/UPnP and mDNS on this worker's network, grouped by address. Each row is
what the device says about itself (server string, description URL, service
types, instance names, ports, TXT records): a lead for which API it speaks.

## Try a draft: `appliance.test`

Write the plugin's `plugin.yaml`, then run one cap from the text before it
ships:

```yaml
cap: light.on
manifest: |
  manifest: 1
  plugin: light
  version: 0.1.0
  appliance:
    settings:
      port: { type: int, default: 80 }
  caps:
    on:
      doc: Switch it on.
      risk: write
      scope: local
      effects: [physical]
      action:
        http: { method: POST, url: "http://{{appliance.host}}:{{appliance.port}}/on" }
instance: { host: 192.168.1.40 }
```

Only network actions (http, tcp, ws) run from a draft; exec and file caps
run once the plugin is installed. A draft can't read the vault: declare the
value as a setting and pass `{{secret:name}}` in `settings` or `instance`.
Testing does what the cap does.

## Ship it

A finished draft is a plugin: the hub signs and pushes it, and records the
appliance instance (its name, delegate and settings). That registry lives on
the hub.
