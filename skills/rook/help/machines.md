---
title: Running things on machines
pitch: rook_call for bounded commands, consoles for long or interactive work, finding workers and caps, output budgets, timeouts
tools: [rook_call, rook_workers, rook_caps, rook_journal, rook_console_open, rook_console_read, rook_console_write, rook_console_close, rook_console_list, rook_console_search, rook_console_signal]
caps: [shell., file., proc., caps.describe, info., customcap.]
refs: [references/usage.md, references/troubleshooting.md, references/tools.md]
notes:
  - rook_console_open: Title the console with the goal, not the command. Read with limit=40-80 and since_seq; close it with rook_console_close(summary=...): the summary is what later searches find.
  - rook_console_read: Defaults to 300 lines. Pass limit=40-80 and page with since_seq, or tail=true to attach late.
  - rook_journal: The journal has the output of earlier and timed-out calls; read it instead of re-running.
  - timeout: Timed out does not mean it didn't run. Before retrying anything with side effects, read rook_journal(call_id=...) and check the effect on the worker; read-only calls are safe to retry.
  - rook_workers: Filter the roster (name=, fields=); never list it unfiltered on a timer.
---
# Running things on machines

**Use it when** you need something done on a machine: run a command, read or write a file, check a service.

## Which way

- **Quick and bounded** (finishes inside its timeout): `rook_call(worker=…, cap="shell.exec", args={"argv": […]})`. Default timeout 30 s; raise `args.timeout` for slow-but-bounded work. Prefer `argv` to `cmd` (no quoting bugs). Windows workers run `cmd.exe`.
- **Slow, interactive (`pty=true`) or worth finding later**: `rook_console_open` → `rook_console_write` / `rook_console_read` → `rook_console_close(summary=…)`.
- **Done before?** `rook_console_search("<task words>")`, then a knowledge search.
- **Who has a cap / what is a worker?** Call your best guess (the refusal names the holders), or `rook_workers(name=…)`, `rook_caps(prefix=…)`, `rook_call(worker=…, cap="caps.describe", args={"prefix": "shell."})` on one worker for exact args.

## Hygiene

- **Bound output at the source.** `shell.exec` has no output cap and `file.read` defaults to 8 MiB: `| tail -n 30`, `head`, `grep -c`, `jq`, `max_bytes`. `text=true` on `rook_call` returns plain stdout.
- **Batch.** Independent checks on one box go in one `shell.exec` with `echo "== label"` separators; calls to different workers run in parallel. Twenty one-liners on one box should have been one call or a console.
- **Don't poll.** Wait inside the worker (`timeout 180 sh -c 'until …; do sleep 3; done'`) or use a job.
- **Timeouts.** "no reply within Ns" does not mean it didn't run. Before retrying anything with side effects (installs, restarts, writes, deploys, messages): `rook_journal(call_id=…)`, then check the effect on the worker (`systemctl is-active`, `git log -1`, `ls`). Prefer idempotent commands; never fire the same side-effecting call twice to see if it works now. Read-only calls are safe to retry; a worker that keeps timing out may be off the band (`rook_call(worker=…, cap="info.ping")`).
- **Close every console** with a summary: what you did, what worked, what to watch.
- **Repeat work becomes a cap.** `customcap.add` turns a command you keep running into `cmd.<name>`; tell the user what you added.

## Mistakes

| Symptom | Fix |
|---|---|
| `bad args: unexpected keyword` | Wrong arg name: `shell.exec` takes `argv`/`cmd`, not `command`. `caps.describe` with a prefix gives exact args |
| Timed out, retried, it ran twice | Journal and effect check before any retry |
| Huge replies | Narrow at the source |
