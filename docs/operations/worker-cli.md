# `rook` on a worker machine

Every desktop worker install puts a `rook` command on the machine (Linux, macOS,
Windows PowerShell, Termux). Run with no arguments it opens the terminal
dashboard (`rook band`); with a command it looks after this machine and talks to
the band from here.

```
rook status                  this worker, its band and hub, plugins, last update
rook doctor                  check the setup; says how to fix what is wrong
rook ask "<question>"        ask the home agent; rook chat for a conversation
rook tasks [search words]    the band's task deck, or a task search
rook rooms [room]            chat rooms, or the latest messages in one
rook knowledge search <q>    search the knowledge pages; knowledge get <slug>
rook plugins                 list plugins; plugins enable|disable <module>
rook logs [-f] [--audit]     this worker's recent log lines, or the calls it answered
rook pair <code>             join a band with a pairing code
rook login [--token]         save an API token (or the dashboard login) for the hub
rook completion <shell>      bash, zsh, fish or powershell completion
```

`rook <command> --help` shows each command's options. Output is plain ASCII, so it
works in any terminal; most read commands take `--json`.

## Status

```
$ rook status
  Worker   kitchen-pc  v0.4.2  (running for 2 h)
  Band     home via hub.example.com:443 (WebSocket)
           connected, last heard 4 s ago
  Hub      https://hub.example.com  (device certificate good for 29 days)
  Plugins  15 on: chat, config, enrollment, file_ops, info, log, ...
  Update   build 512 confirmed healthy 2 days ago (band round trip)
  Hub link  your API token (ask, tasks, rooms, knowledge)
```

The band, hub and certificate come from the pairing saved in the worker's state
directory; band contact, loaded plugins and uptime come from the running worker.
"Update" is the last OTA update and its [health gate](../FEATURES.md#post-update-health-gate)
result, or the build being checked right now.

## Doctor

`rook doctor` checks, in order: the Python version and worker program; whether
the machine joined a band (or was removed from it); the device certificate and
whether the key file is private; whether the worker is running; whether the hub
answers; the clock against the hub's (sign-ins and band calls fail when it is
more than a few minutes off); the band link (live contact when the worker runs,
else a TCP probe of the hub address); a rolled-back update; and a settings change
from the hub that is waiting to be confirmed. Each problem comes with a fix:

```
$ rook doctor
Rook doctor: kitchen-pc
  ok    Worker program   v0.4.2, Python 3.12.3
  ok    Band membership  joined home (hub hub.example.com:443)
  ok    Device key       certificate good for 29 days
  FAIL  Worker running   the worker isn't running
                         Fix: Start it: systemctl --user restart rook-band-worker
  ok    Hub reachable    https://hub.example.com answered in 0.2 s
  warn  Clock            95 s behind the hub
                         Fix: Turn on automatic time: timedatectl set-ntp true

1 problem to fix, 1 to look at.
```

It exits 1 when something failed, so scripts can use it.

## How it reaches the hub

`ask`, `chat`, `tasks`, `rooms` and `knowledge` use, in order:

1. **An API token**, when one is saved: create it on the dashboard's **Tokens**
   page, then run `rook login --token` and paste it (or set `ROOK_TOKEN`). Calls
   go to the hub's agent endpoint (`/mcp`, the one Claude Code uses) and run as
   that token, with its permissions and its rows in the journal. This is the way
   to ask the home agent from a terminal. A bare `--url host` means
   `https://host` unless the host is on your network (a private or Tailscale
   address, `localhost`, a name without a dot or ending `.local`/`.lan`, or a
   name whose addresses are all private, like `hub.<tailnet>.ts.net`), which
   gets `http://`. If https meets a hub serving plain http, the error says so
   and gives the `http://` command. A token or password is never sent over
   plain http to a public address without a yes at the prompt (or
   `ROOK_ALLOW_HTTP=1` where there is no terminal), and a redirect from the endpoint is
   never followed (the token would go along): the error names the address to
   use. On a hub whose MCP server runs on its own port, pass
   `--mcp-url http://<hub address>:8765/mcp`.
2. **Your dashboard login**, when one is saved: `rook login` (or the file `rook
   band` saves, `~/.config/rook/band.conf`, or `ROOK_WEB_URL`, `ROOK_WEB_USER`,
   `ROOK_WEB_PASS`). Reads run as you, and rooms are your rooms. The dashboard
   sends these calls over the band, so the band's limit below applies too.
3. **This worker's band link**, through the running worker. Calls run as this
   worker. The hub answers band calls only up to the `read` risk tier by default
   (the hub setting `hub.band_max_risk`): reads work, asking the home agent does
   not, and the command says how to fix that.

`--via token|login|band` picks one. The home agent answers whole replies (no
streaming); `rook chat` passes the last few turns along as context, since the
agent keeps no memory between questions. `rook ask -` reads the question from
standard input.

## Plugins

`rook plugins` lists this worker's plugins: on, off (and whether the band's plugin
choices switched it off at pairing), or unavailable with the reason. `rook plugins
enable|disable <module>` switches one through the running worker, persisted
exactly like the dashboard's Plugins page (`plugins.json`); with the worker
stopped it edits that file and the change applies at the next start. The hub's
rules hold here too: `selfupdate`, `config`, `enrollment` and `info` stay on (they
are how the hub updates, configures and reaches the worker), and a later switch
on the dashboard replaces a local one.

## Logs

`rook logs` prints the running worker's recent log lines (INFO and up, kept in
memory; `-f` follows). That works everywhere, including Windows, where the
worker runs without a console. With the worker stopped it falls back to the
service's journal (`journalctl --user -u rook-band-worker`) or the worker's log
file. `rook logs --audit` lists the calls this worker answered and who made them.

## Pairing

`rook pair <code>` joins a band with a pairing code from the dashboard's Install
page, like the installer does (`rook worker --enroll <hub> --pair-code <code>`).
It needs the hub's address once (`--hub http://192.0.2.10:7005`, or
`ROOK_SERVER`); after that it reuses the hub this machine joined. A running
worker moves to the new band within a minute.

```
$ rook pair x7k2pq --hub http://192.0.2.10:7005
Joining a band with pairing code x7k2pq
  1. Made a private key for this machine
  2. The hub accepted the code: band "home"
  3. Saved the band settings where only you can read them
Done: kitchen-pc joined home
```

Nothing prompts without a terminal. With an account sign-in instead of a code,
an account with several bands needs `--band NAME` (or `ROOK_BAND`) when no one
is there to choose.

## Shell completion

```sh
eval "$(rook completion bash)"        # ~/.bashrc
eval "$(rook completion zsh)"         # ~/.zshrc
rook completion fish > ~/.config/fish/completions/rook.fish
rook completion powershell | Out-String | Invoke-Expression   # in $PROFILE
```

## A hotkey for the home agent

The desktop app has a global hotkey for the home agent. Without it, any desktop
can bind a key to a terminal running `rook chat`: on KDE or GNOME add a custom
shortcut for `konsole -e rook chat` (or `gnome-terminal -- rook chat`); on Windows
make a shortcut to `powershell -NoExit -Command rook chat` and set its "Shortcut
key" in Properties.

## The local control link

The running worker listens on `127.0.0.1` (a random port) for these commands and
writes the port and a random token to `control.json` in its state directory
(mode 600; on Windows an owner-only ACL, as for the saved API token). Reading
that file is the credential; whoever can read it can already
read the band key beside it, so the link gives nothing extra. It runs only the
plugin switches locally; everything else it sends on the band as this worker.
A connection must send its token within 2 seconds, and only connections whose
token passed count against its limit of 8 at once, so idle connections from
another program can't lock the commands out.
`ROOK_CONTROL=0` turns it off (status then shows only what the files say).
