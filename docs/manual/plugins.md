---
id: plugins
title: The Plugins page
order: 16
features:
  - settings/core/plugins
  - plugin/hub/marketplace
since: 0.1.0
deprecated: false
---
# The Plugins page

Everything Rook can do comes from plugins. **Plugins** (under Manage, operator
only) shows which plugins are on, on the hub and on every online machine, and
lets you switch them.

## The grid

Rows are plugins; columns are the hub and each machine (filter by band or
operating system). Each cell says **on**, **off** or **not available**, and why:
for example the machine has no screen, a program is missing, or the plugin
only runs on the hub. Click a cell to switch that plugin on that machine right
away. On a phone-sized screen the page shows a summary per plugin instead.

Click a plugin's name for its details: what each of its capabilities does and
how risky it is, a link to its settings, **switch on every eligible machine**
at once, and **reload**.

A few core plugins always stay on (updates, configuration, enrollment and
machine info on workers; settings, permissions and info on the hub).

## Presets: "What do you want Rook to do?"

Presets switch groups of plugins in one go: remote control, agent sessions,
knowledge and tasks, home and voice agent, phone and chat alerts, hardware.
Each shows how risky it is and calls out its high-risk parts. You see the same
choices on the first-worker card and the Install page.

## Defaults for new machines

Per band, choose which plugins new machines start with. A machine applies these
on its first start only; after that its own switches win. A pairing code
carries the defaults only when you attach them.

## Hub plugins

Switching a hub plugin is remembered across restarts. Switching on a plugin
that has an **Enabled** setting turns that on too, unless an environment
variable decides it.

**Advanced security** is a hub plugin, off by default. On, it brings back
the stricter holds and the votes before merges and deploys
([[manual-security]]). Only you can switch it; agents are refused.

## Marketplace (preview)

The **Marketplace** tab lists plugins from plugin catalogs (an example catalog
of made-up entries when none is set; add catalog addresses in the **Plugin
catalog URLs** setting). Installing from the marketplace is not available yet;
the page says so and explains the manual way.
