---
id: worker-commands
title: The rook command on a worker machine
order: 23
features:
  - features/rook-on-a-worker-machine
since: 0.1.0
deprecated: false
---
# The rook command on a worker machine

Every desktop worker install adds a `rook` command to that machine (Linux,
macOS, Windows PowerShell and Termux). On its own, `rook` opens the terminal
dashboard ([[manual-terminal-and-commands]]). Followed by a command, it looks
after the machine you are sitting at and talks to your band from there: check
that the worker is healthy, fix common problems, ask the home agent, and read
tasks, rooms and knowledge pages without opening a browser.

`rook <command> --help` shows each command's options. Most commands that read
something also take `--json`.

## Is this machine all right?

- `rook status` shows this worker's name and version, the band it joined and
  how it reaches the hub, whether it is in contact right now, which plugins are
  on, and how its last update went.
- `rook doctor` checks the setup step by step: the worker program, the band
  membership, the machine's certificate and key, whether the worker is running,
  whether the hub answers, the clock, the band link and the last update. Every
  problem comes with a plain fix to try, for example the command that starts
  the worker again. It ends with a short summary and exits with an error code
  when something failed, so scripts can use it too.

## Asking the home agent

`rook ask "what is on the NAS?"` asks the home agent ([[manual-home-agent]])
and prints its answer. `rook chat` keeps a conversation going, passing the last
few turns along; type `/exit` to stop. `rook ask -` reads the question from the
keyboard or a pipe.

The home agent answers through an API token. Create one on the dashboard's
**API tokens** page ([[manual-agents-and-mcp]]), then save it on this machine:

```sh
rook login --token
```

Paste the token when asked (or set `ROOK_TOKEN`). Calls then run as that
token, with its permissions, and appear in the journal under its name.

Without a token, `rook` uses your saved dashboard login (`rook login` saves it;
the terminal dashboard uses the same file), and failing that the worker's own
band link. Both can read, but by default they can't ask the home agent: the
command then tells you to save a token.

## Reading tasks, rooms and knowledge

- `rook tasks` shows the band's task deck; `rook tasks <words>` searches it
  ([[manual-work]]).
- `rook rooms` lists your chat rooms; `rook rooms <room>` shows the latest
  messages in one ([[manual-chat]]).
- `rook knowledge search <words>` searches the knowledge wiki, and
  `rook knowledge get <page>` prints a page ([[manual-knowledge]]).

## Plugins and logs

- `rook plugins` lists this worker's plugins: on, off, or unavailable and why.
  `rook plugins enable <name>` and `rook plugins disable <name>` switch one,
  exactly like the switches on the dashboard's Plugins page
  ([[manual-plugins]]). The few plugins the hub needs to update, configure and
  reach the worker always stay on.
- `rook logs` prints the worker's recent log lines; `rook logs -f` keeps
  following them. This also works on Windows, where the worker has no window.
  `rook logs --audit` lists the requests this worker answered and who made
  them.

## Joining a band from the terminal

`rook pair <code>` joins a band with a pairing code from the dashboard's
**Install a worker** page, the same way the installer does
([[manual-adding-machines]]). The first time, add the hub's address:

```sh
rook pair x7k2pq --hub http://192.0.2.10:7005
```

It prints numbered steps and ends with "Done: <machine> joined <band>". A
worker that is already running moves to the new band within a minute.

## Signing in safely

`rook login` and `rook login --token` ask for the hub's address the first time
(`--url`; by default the hub this machine joined). An address without
`http://` or `https://` means `https://`, unless it is on your own network (a
private or Tailscale address, `localhost`, or a name only your network knows),
which means `http://`. If the hub turns out to serve plain http, the error
says so and gives the command to use.

Your token or password is never sent unencrypted across the internet without
your say-so: for a plain `http://` address outside your network, `rook login`
asks first, and refuses when there is no one at the keyboard (set
`ROOK_ALLOW_HTTP=1` if you really mean it). It also never follows a redirect
with your token; the error names the address to use instead. On a hub whose
agent endpoint runs on its own port, add
`--mcp-url http://<hub address>:8765/mcp` to `rook login --token`.

The token is saved in `~/.config/rook/cli-token.json`, readable only by you.

## Tab completion

`rook completion bash` (or `zsh`, `fish`, `powershell`) prints a completion
script for your shell, for example in `~/.bashrc`:

```sh
eval "$(rook completion bash)"
```

## How it talks to the worker

The running worker listens for these commands on the machine itself only
(`127.0.0.1`), with a secret kept in its settings folder that only your user
can read. Anything that could already read that folder could already act as
the worker, so this adds no new way in. Set `ROOK_CONTROL=0` to switch it off;
`rook status` then shows only what the files say.
