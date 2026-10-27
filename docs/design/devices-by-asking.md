# Devices by asking: the house agent builds the caps

Status: implemented (`devkit` worker plugin, help topic `devices`, one hub
hook). Builds on the device framework in [devices.md](devices.md). User
manual: `docs/manual/devices.md`, "Adding a device by asking".

## 1. Goal

Bake (roadmap, 2026-10-26): "add my front door lock" and the house agent
builds the caps itself, instead of one integration per device (the Home
Assistant way). Most things in a house already have a small local API: an
HTTP endpoint, a line protocol on a TCP port, a UDP packet, a vendor CLI.
What's missing is a way for an agent to turn what it learns about one into a
device worker without anyone writing a driver.

Non-goals: cloud-only devices behind an account login flow (OAuth), devices
that need a long-lived connection or push events (those still want a real
driver, like `webos`), and arbitrary code from agents.

## 2. Model

- **A definition, not code.** `devkit.define` takes a JSON definition:
  `name`, `type`, an optional stable `key` (default: the name, lowercased),
  `vars` (constants such as `host`), `caps`, and optional `state`, `model`,
  `description`. Each cap is one *recipe*:
  - `http`: method, url, headers, query, body (JSON or text), `auth` (basic
    or digest), `insecure` (a device's self-signed certificate), `ok`
    (statuses that count as success), `timeout`;
  - `tcp`: host, port, `send` (text) or `send_hex`, `read` the reply;
  - `udp`: host, port, `send` / `send_hex`, wait for one `reply`;
  - `argv`: a program run without a shell, with `env`.
  Every recipe can `extract` from the reply: a dotted path into JSON
  (`state.locked`, `devices.0.level`), `re:<regex>` on text, `$status`, or
  an object of several. Caps declare `args` (`str`, `int`, `float`, `bool`,
  with `min`/`max`/`choices`/`default`), checked and typed on every call.
- **Placeholders.** `{arg}` and `{var}` in any string; a JSON body string that
  is exactly `{arg}` takes the arg's typed value. Caller args are
  URL-quoted in the url (they can't change where a request goes); vars go in
  as written (they are the address). Where a recipe goes (the url's host
  and port, a tcp/udp host and port) comes from `vars` only: a caller can't
  point a cap, and the credential it carries, at another host. An argv value
  from a caller may not start with `-`, and the program gets a small
  environment (PATH, HOME, locale, temp dirs) plus its own `env`, never the
  worker's.
- **A device per definition.** `RecipeDevice` is a `rook.core.devices.Device`
  whose namespace and type are the definition's `type`, so "front-door"
  shows on the band with `lock.state`, `lock.lock`, `lock.unlock`, a
  `device` block `{type: lock, driver: devkit, model}` and its own derived
  worker id (stable across restarts and renames: it comes from the key).
  Its caps are built from the recipes (`Device.caps()` returns handlers that
  carry `CapMeta` and a signature, so `caps.describe`, tiers and the
  dashboard's call form work as for any device). No change to the device
  framework was needed.
- **State.** `state: {cap, every}` names a read cap that needs no args; the
  plugin runs it every `every` seconds (15-3600, default 60) in the
  background and the heartbeat carries `{on: <reachable>, …}` plus up to six
  short scalars from the reading (`hb.lock = {on, locked, battery}`).
  Heartbeats never do I/O. A device without `state` has no heartbeat.
- **Lifecycle.** Definitions live in the plugin's data directory
  (`devices.json`, mode 600, no secret values in it) and come back on
  restart. Define and remove re-announce at once, so a device appears or
  leaves within a second. A replaced or removed definition goes to
  `retired.json` (last 50): put one back by defining it again.

## 3. Risk is honest, with a floor

A definition can't make a cap look safer than what it does:

| Recipe | Lowest risk |
|---|---|
| http GET / HEAD | read |
| a GET/HEAD cap named for an action (`turn_on`, `disarm`, `set_level`, `lock`) | write |
| any other HTTP method, tcp, udp | write |
| argv (runs a program) | exec |

A declared `risk` only raises it. Every cap that is not a read is also tagged
`physical`: it acts on a thing in the house, the convention `webos` already
follows. The agent declares `destructive` and `sensitive` where they apply
(a camera snapshot is a sensitive read). From there the existing machinery
decides (trust map, approvals, hard stops); this plugin adds no holds of its
own. Unlocking a door or opening a garage is declared `exec` (see the help
topic), which the trust map treats as it treats any exec.

Management caps: `devkit.define` and `devkit.remove` are **admin** (they
create or take away callable surface and set its tiers for everyone);
`devkit.test` is **exec** (it runs any recipe, including argv);
`devkit.list` and `devkit.discover` are reads. All carry `scope=`.

## 4. Credentials

A definition names a credential as `{secret:<vault name>}` with **single
braces**. The double-brace form is the hub's: in `rook_call` args the hub
substitutes `{{secret:…}}` with the value before the worker sees it, so a
definition written that way would store the credential on the delegate's
disk. `normalize` refuses `{{secret:` outright, and refuses a literal value in
any credential-looking place (an `Authorization`/`Cookie`/`*token*`/`*key*`
header, a `password`/`token`/`api_key` query or body key or env var, a
user:password in the url, `auth.password`): those must be a placeholder.
`{secret:…}` is not allowed in argv (a command line is visible to every
user of the machine); pass it in `env`.

At use, the delegate fetches the values from the hub with the existing
`settings.worker_secret` request and keeps them in memory for five minutes.
The hub answers only for names **granted** to that worker: today
`worker_secrets` allows vault entries referenced by a secret worker setting;
it now also allows the names listed in the worker setting `devkit.secrets`
(`SECRET_GRANT_KEYS` in `rook/hub/settings_service.py`, the one hub change).
That setting is written with `settings.set` (admin) like any other, and the
hub accepts only names of vault entries that exist (a `{{secret:…}}` written
into that call would arrive filled in, and is refused rather than stored), so a
person (or an agent the trust map lets) decides which delegate may use which
credential, and a worker still can't fetch a secret nobody granted it. Any
value a device echoes back is masked as `{secret:name}` in replies, and the
hub's own reverse masking applies on top.

Why not store the value on the delegate (as `webos` stores its pairing key)?
The vault stays the one place a credential lives: rotating it there takes
effect within five minutes, nothing is written to the delegate's disk, and
every fetch is in the vault's access log.

## 5. The house agent's half

The skill lives in the help topic `devices` (`skills/rook/help/devices.md`)
and a short pointer in the house agent's own instructions
(`rook/hub/plugins/home/roledir.py`, `DEVICE_RULES`). The steps:

1. Find out what the device is (make, model) and where (its address), asking
   the person only what it can't find: `devkit.discover` on a delegate lists
   what answers SSDP and mDNS on that network.
2. Find its local API: the maker's docs, the web, the device's own pages.
3. Pick the delegate: an always-on worker on the same network.
4. Credentials: ask the person to store them under **Secrets** (the agent
   never handles the value), then grant the vault name to the delegate
   (`settings.set devkit.secrets`), which the trust map treats as the admin
   change it is.
5. Draft the definition; `devkit.test` each read cap until the replies and
   extracts are right; test action caps only with the person's go-ahead
   (testing unlock unlocks).
6. `devkit.define`, check the device on the band (`rook_workers`), call its
   state, and tell the person what they can now say.

## 6. Later

- A dashboard status for non-TV devices (today the worker list shows `on` /
  `off` from `hb.<type>.on`; the extracted state is in `hb` and `devkit.list`).
- Long-lived connections and push (MQTT, websockets, a lock that reports
  "unlocked by keypad"): a `subscribe` recipe kind feeding the heartbeat.
- Sharing definitions: a definition is plain JSON; a knowledge page of known
  good definitions per model would let the next house skip the research.
