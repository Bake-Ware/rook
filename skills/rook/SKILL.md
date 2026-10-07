---
name: rook
description: Operate a Rook band (workers, hub, MCP) efficiently: running commands on machines, consoles, chat, tasks, knowledge, secrets, config and OTA. Use whenever rook_* MCP tools are available or the user mentions Rook, a band, a worker by name, or installing/administering Rook. Covers install, configuration, administration and token-lean usage patterns.
---

# Rook

Rook is a mesh of **workers** (one per machine) that dial out to a **hub** and join an encrypted **band**. Agents reach them through the Rook MCP: `rook_call(cap, args, worker)` runs a **capability** (`shell.exec`, `file.read`, …) on a named worker and returns the reply. Everything is journaled under your identity.

Read the reference that matches the job; don't load them all:

| Job | Read |
|---|---|
| Installing a hub, worker, Android app, or connecting an MCP client | `references/install.md` |
| Config changes, OTA, plugins, hub services, tokens, bands, troubleshooting | `references/admin.md` |
| Exact tool and cap arguments (generated from code) | `references/tools.md` |
| Consoles, custom caps, coordination, lean output recipes | `references/usage.md` |
| This band's site notes (host roles, standing rules), if the hub provides them | `references/site.md` |

`references/site.md` is written by the hub operator and only exists when the hub serves one. If it is missing, there are no site notes; don't go looking for them.

## The five rules that save the most tokens

1. **Filter the roster instead of dumping it.** `rook_workers(name=, cap_prefix=, online=, fields=)` and `rook_caps(prefix=, worker=)` return compact views, but unfiltered they still grow with the fleet. If you know roughly where something runs, just call it: a refused `rook_call` lists the workers that *do* have the cap, the cheapest discovery there is. For one host's details use `rook_call("info.host", worker=X)`.
2. **Bound every output at the source.** `shell.exec` has **no output cap** and `file.read` defaults to **8 MiB**. Pipe through `head`/`tail`/`cut -c1-200`/`grep -c`/`wc -l`, pass `max_bytes` to `file.read`, and `data.limit` to knowledge/task searches. Shape JSON on the worker (`jq`, `python -c`) and return only the fields you need. `rook_call(..., text=true)` returns `shell.exec` stdout as plain text (stderr and exit code only when set).
3. **One call, many commands.** Batch independent checks into one `shell.exec` with `;` and `echo "== label"` separators. Issue independent `rook_call`s to *different* workers in parallel.
4. **Don't re-run to re-read.** A reply's `id` is its journal id; `rook_journal(call_id=id)` returns the stored output. Long jobs go in a console room; re-attach with `rook_console_read(room, tail=true, limit=40)`.
5. **Search before you rediscover.** `rook_console_search("<task words>")` finds how something was done before; `rook_knowledge(query=…, data={"limit":3})` finds facts.

## Calling capabilities well

- **Target by name** (`worker="worker-a"`). `worker` is required; ids can change, names don't.
- **Prefer `argv` over `cmd`.** `argv=["systemctl","status","x","--no-pager"]` needs no quoting. Use `cmd` only for pipes/redirection.
- **Timeouts:** `shell.exec` defaults to 30 s; raise `args.timeout` for slow-but-bounded work (rook_call waits for it). Minutes-long, interactive or worth keeping → `rook_console_open`.
- **Wait inside the worker, not by polling:** `cmd: "timeout 180 sh -c 'until systemctl is-active -q svc; do sleep 3; done'"` with `args.timeout: 190` is one call instead of ten.
- **Write files with `file.write`** (`create_parents`, `encoding: "base64"` for binary), not heredocs through `cmd`.
- **Read slices:** `grep -n pattern f | head`, then `sed -n 'A,Bp' f`.
- **Secrets never enter your context.** Put `{{secret:name}}` in `rook_call` args; the hub substitutes and masks it. `rook_secret(action="list")` shows names only.
- **Notices** ride `rook_call` replies only when new for your session: `_tips` (a cap's usage tip, once), `_task` (the claimed task the call was recorded on) and `_unread_chat` (when it changes). Pass `hint=true` only if you need a tip again.
- **Screens:** on Android prefer `ui.text` over `screenshot.capture`; on desktops try `screenshot.capture_preview` or `capture_region` first.
- **Windows workers** run `cmd.exe` (no `grep`/`sed`/`head`; use `powershell -NoProfile -Command "…"`) and `proc.*`/console rooms have no PTY there; on Windows 10 1809+ workers with `work.stream.*` (ConPTY), use those caps for interactive terminals, otherwise a WSL worker.
- **Repeated multi-step work** becomes a custom cap (`customcap.add` → `cmd.<name>`); see `references/usage.md`.

## Coordination, briefly

- Resuming? `rook_task(action="deck")` or `rook_handoff_list(limit=5)`; heed `freshness` (`STALE`/`SUPERSEDED` means verify first).
- Claim before working (`rook_task(action="claim", id=…)`); finish with state `done`, `attrs.outcome` and an evidence link. Stopping mid-way needs a handoff.
- Writes to tasks/projects/knowledge need a fresh `request_id`.

## Safety and etiquette

- The band PSK is root on every worker. Never print it or put it in args, chat, knowledge or handoffs.
- Destructive or fleet-wide actions (hub service restarts, `worker.deauth`, PSK rotation, band migration, mass `worker.apply`) need the user's go-ahead. Restarting the MCP service disconnects every agent, including you.
- Config pushes go through `rook_config_apply` (commit-confirmed, auto-reverts); don't hand-edit a worker's config over `shell.exec`.
- Agents cannot mint pairing codes (dashboard **Tokens** page only) or sign updates/deauths (controller key only). Ask the user.
- Leave a trail: close consoles with summaries, link evidence to tasks, save a handoff after substantial work.
