# Notify

`notify.send` gets a message to a person on a channel that reaches them. It is
the hub's one send service (the `notify.send` contract); agents, jobs and
plugins call it instead of talking to Telegram or a phone themselves. The
house agent decides when and how to reach someone and can name the channel.

| Channel | How it reaches the person | What it needs |
|---|---|---|
| `phone` | `notification.post` on their device (any worker that provides it: the phone app, a desktop, a TV) | their `phone` setting: the worker's name |
| `telegram` | a direct message from the band's bot | the telegram plugin, and the person linked (`telegram.link`) |
| `discord` | a direct message from the band's bot | the discord plugin, and the person linked (`discord.link`) |
| any other | whatever plugin provides `notify.channel.send` (named by its plugin) | that plugin |

A channel plugin provides the `notify.channel.send` and `notify.channel.status`
contracts; notify finds it in the hub's registry and calls it through them, so
a new channel needs no change here.

## Calling it

```
notify.send {text: "The backup finished.", to: "bake"}
notify.send {text: "Dinner is ready", to: "kid", channel: phone, title: Home}
notify.send {text: "Deploy is waiting on you", channel: all}      # every channel the owners have
```

- `to` is a person's name; blank reaches each of the hub's owners.
- Without `channel` the channels are tried in the person's own order (their
  `reach` setting), else the hub's (`channels`), and the first that works
  takes it. `channel: all` sends on every channel that works.
- The result lists `sent` (`{to, channel}`) and `errors` (why a channel didn't
  go). If nothing went anywhere the call fails with those reasons.
- Every channel call runs as the caller, through the contract, so their
  rights apply: a grant of `notify.channel.send` (or `telegram.send`) lets
  them reach people by Telegram; without one they can't.

`notify.channels {to}` shows the order and, per channel, whether it is
installed and can reach that person now (linked, connected).

## Settings

| Setting | Where | Meaning |
|---|---|---|
| `channels` | hub | the order for people who haven't set one (blank: phone, then every installed channel) |
| `reach` | person | your own order |
| `phone` | person | your phone's worker name |

A person sets their own with `setting.set {plugin: notify, key: phone, value: pixel}`; owners
can set anyone's (`person: kid`).
