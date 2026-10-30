# Shell

Runs commands on this worker, reads its environment, and opens live
terminals. A worker without this plugin can't run arbitrary commands at all,
which is stronger than any permission rule (a NAS can carry only `file`).

| Cap | What it does |
|---|---|
| `shell.exec` | Runs a command and waits: `argv` (a list, no shell, no quoting problems) or `cmd` (a string for a shell). Returns `ok`, `code`, `stdout`, `stderr`. `timeout` (30 s by default, up to an hour) kills the process and everything it started. |
| `shell.which` | Full path of a program on PATH, or null. |
| `shell.env` | One variable (`name`) or all starting with `prefix`. Names that look like credentials (`*TOKEN*`, `*SECRET*`, `*PASSWORD*`, `*_KEY`, `ROOK_*`) read `***`. |
| `shell.open` | Starts a command as a live stream and returns `{stream}`: a login shell by default, on a terminal (`pty=true`) or on pipes (`pty=false`, what `proc.start` was). |

## Shells

`cmd` runs in `/bin/sh -c` on Linux and macOS and in PowerShell on Windows
(PowerShell 7 when installed), so `ls`, `cat`, pipes and `$env:X` mean
something everywhere. `shell` picks another: `sh`, `bash`, `powershell`,
`cmd` (Windows only). On Windows, `sh` and `bash` are Git for Windows' own,
never WSL's. A reply names the shell unless it was plain `sh` on Linux or
macOS.

On Windows `argv[0]` is found the way a shell finds it: PATHEXT (`npm` is
`npm.cmd`), npm shims run their real target, and `python3` that only finds
the Microsoft Store stub runs a real Python.

## Elevation

Every process starts through the worker's launch path: as the worker's
normal user, or with `sudo -n` when the call carries an elevation lease.
`shell.exec` and `shell.open` may be elevated, but only when the caller asks
for it: a command that fails with "permission denied" comes back with
`denied: true` and is never re-run on its own, because it may have half-run.

## Terminals and processes

`shell.open` opens a `terminal` stream (`pty=true`) or a `process` stream
(`pty=false`, stdout and stderr merged). Its output arrives as `bytes`
events; send it input with `stream.write`:

- `{text: "ls\n"}`: keystrokes (on a terminal, `"\u0003"` is Ctrl-C).
- `{signal: INT | TERM | HUP | QUIT | KILL}`: to the process and its children.
- `{resize: [cols, rows]}`: the terminal size.

The stream closes when the process exits, with `{exit: <code>}` (a negative
code is the signal that ended it). Unloading the plugin ends every process
it started. The buffer is bounded (the newest 256 KiB); export a stream to
keep it.

Windows: `pty=true` isn't available yet (ConPTY isn't ported); `pty=false`
works.

Old names: `shell.env.get` still answers.
