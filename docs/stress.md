# Stress test of core (brief 48)

Bake, 17 November: "is this new core reliable? Is there a way to flood test it?" This is the answer for core as it stands on `e76f9018` (pull requests up to 431 merged). Nothing in the product was changed to run it and nothing was fixed; the faults found are below with the steps to see them again.

## How it was run

- **Where.** cachyrig, Bake's desktop: 12 cores, 62 GB, CachyOS (Linux 6.18), Python 3.14.5, load average 1.1 to 2.0 from other work while the rounds ran. Never against rookbeta.bake.systems.
- **What.** For each round, a hub and Python workers are started from this clone with the README's own commands (`python -m rook_hub --data … --vault-key … --port 0`, `python -m rook_worker URL`), each worker with its own `HOME`, so its state is in its own directory. All of it lives in a new directory under `/tmp`, removed when the round ends. The first user is made on the setup page (`POST /api/setup`) and the agent token minted on the Auth page (`POST /api/tokens`), as a person does; workers are approved through the door (`worker.approve`).
- **How the load is put on.** Everything an agent does goes through `POST /mcp` with the agent token, one JSON-RPC message per request, as an MCP client sends it. A call that answers `running` after 45 s is followed on its stream to the end, as the door's instructions say. The client has no timeout of any kind and no limit on connections (aiohttp `ClientTimeout(total=None, connect=None, sock_read=None, sock_connect=None)`, `TCPConnector(limit=0)`), so a call that never answers would keep the round waiting: none did (see "Forever-waits found").
- **Measured.** Before and after every round, and during the long ones, each process's RSS, open file descriptors, threads and CPU seconds, read from `/proc`. "Answered" counts every request the door answered, an error included; "no answer" is a request whose HTTP exchange itself failed.
- **The scripts** are in `tests/stress/` (README there: one line per round). Each writes its numbers as JSON lines. pytest collects nothing there.

A note on file descriptors: the hub's count after a flood is mostly the client's idle keep-alive connections (one per request that was in flight at once), which the hub keeps while the client does. They are not a leak: they go when the client closes.

## Summary

RSS is the hub's, then each worker's, in MB, before and after.

| Round | Load | Answered | Failed, by code | Slowest | RSS before → after |
|---|---|---|---|---|---|
| 1 | 200 `shell.exec` at once, 3 workers | 200 / 200 | none | 0.30 s | hub 52 → 64; workers 38 → 39 |
| 1 | 1,000 at once | 1,000 / 1,000 | none | 1.34 s | hub 64 → 94; workers 39 |
| 1 | 5,000 over a minute | 5,000 / 5,000 | none | 0.11 s | hub 94 → 96; workers 39 |
| 1, past the brief | 5,000 at once | 5,000 / 5,000 | none | 6.23 s | hub 96 → 324; workers 39 |
| 1, past the brief | 20,000 at once (first run) | 16,900 / 20,000 | `worker_disconnected` 10,070, `worker_offline` 652, no answer (ClientOSError) 3,100; 6,178 ok | 122 s | hub 324 → 541; workers 39 |
| 1, past the brief | 20,000 at once (fresh hub) | 20,000 / 20,000 | `worker_disconnected` 19,997; 3 ok | 31 s | hub 52 → 1,276; workers 38 |
| 2 | 25 terminals, typed and read every 100 ms, 5 min | 148,850 / 148,850 | none; 74,400 echoes typed, 74,400 back, in order | 0.058 s | hub 52 → 56; worker 38 → 39 |
| 2 | 100 terminals opened and closed, 10 times | 2,000 / 2,000 | none; 0 left open | 0.051 s | hub 56 → 61; worker 39 → 40 |
| 2, past the brief | 1,000 terminals at once, one echo each | 5,014 / 5,014 | none; 1,000 echoes back | 0.86 s | hub 65 → 115; worker 43 → 55 |
| 3 | one stream, 100,000 lines | 10 reads | none; whole, in order | 0.016 s a read | hub 52 → 53; worker 39 |
| 3 | one stream, one 50 MB write | 764 reads | none; 50,000,000 bytes, whole | 0.024 s a read | hub 53 → 55; worker 39 → 97 |
| 3 | ten streams at once, 10,000 lines each | all | none; all whole, in order | 0.056 s a read | hub 55; workers 97, 38, 38 |
| 3, past the brief | 100 streams at once, 10,000 lines each | 305 reads | none; 100 of 100 whole | 0.69 s a read | hub 52 → 65; workers 38 → 43 |
| 4 | results of 1, 10, 15, 20 MB, text and bytes (16 calls) | 16 / 16 | `cap_failed` 6: every result over 16,000,000 bytes encoded | 2.08 s | hub 52 → 84; worker 38 → 171 |
| 5 | a worker linked and dropped (SIGKILL) 500 times | 500 relinks | none; one worker listed, call answered after | 0.16 s a relink | hub 52 → 55; worker 38 |
| 5 | three workers heartbeating every 5.6 ms, 10 min | 180 / 180 calls | none; 321,532 heartbeats | 0.031 s | hub 55 → 58; **data 4.9 → 3,319 MB** |
| 5 | 50 fresh keys knocking at once | 50 / 50 approved | none; all online in 1.75 s | | hub 58 → 62 |
| 6 | clean stop and start under load | 6,100 calls; 6,068 answered | `worker_offline` 1,531 (15 s each restart), `hub_restarted` 7, no answer 32 (in flight at a stop, or sent while the hub was down) | 0.21 s | hub 56 → 53 |
| 6 | kill -9 and start under load | (same run) | | | hub 54 → 54 |
| 7 | 20 calls/s, 5 terminals, 1 stream, one hour | 72,000 / 72,000 calls; 357,010 / 357,010 terminal requests; 35,493 / 35,493 stream reads | none | 0.124 s | hub 52 → 58; worker 38.5 → 38.9; data 1.7 → 120 MB |
| 8 | 100,000 journal entries and 50,000 records, read 20 ways | 20 / 20 | none | 2.1 s (whole journal); others wait up to 2.0 s | hub 52 → 178 (235 at most) |

## Round 1: call flood

`tests/stress/flood.py`. Three workers; calls go round them in turn; ten short commands in turn (`echo`, `true`, `date`, `seq 1 100`, `uname -a`, `ls /`, `sleep 0.1`, `printf`, `exit 3`, a write to stderr).

| Load | Wall | Median | 99th | Slowest | Answered |
|---|---|---|---|---|---|
| 200 at once | 0.32 s | 0.21 s | 0.28 s | 0.29 s | all, all `ok` |
| 1,000 at once | 1.44 s | 0.65 s | 1.32 s | 1.34 s | all, all `ok` |
| 5,000 over 60 s | 60.06 s | 0.005 s | 0.106 s | 0.108 s | all, all `ok` |
| 5,000 at once | 6.46 s | 4.23 s | 6.14 s | 6.23 s | all, all `ok` |
| 20,000 at once | 123 s | 22 s | 122 s | 122 s | see below |

The brief's three loads pass with nothing lost: no call was followed as `running`, no error, and the workers' RSS did not move.

**Where it gives: about 10,000 calls in flight at once.** The brief's numbers did not find a limit, so I went on. 7,500 `worker.ping` calls at once over three workers all succeed (8.5 s); 6,000 to one worker succeed (6.7 s). From 10,000 on, one worker or three, almost nothing is answered:

- 10,000 `worker.ping` at once to one worker: 4 `ok`, 9,996 `worker_disconnected`, in 13.7 s. 10,000 and 15,000 over three workers: 4 `ok` each, the rest `worker_disconnected`. 20,000 `shell.exec` on a fresh hub: 3 `ok`, 19,997 `worker_disconnected`.
- The workers never get the calls: in a 20,000 `worker.ping` run their CPU time went from 0.1 s to 0.2 s while the hub's went from 0.3 s to 15 s and its RSS from 52 MB to 1,270 MB in 14 s.
- About 10 to 30 s in, the hub's Telesthete logs `Channel 0: peer unresponsive after 10 retransmits; tearing down` (or `Peer timeout: cachyrig`), each worker says `The link to the hub was lost; linking again.` and links again within 1 to 3 s by itself. Every call in flight on the dropped link is answered `worker_disconnected`; any that arrive before the new link, `worker_offline`.
- A profile of the hub (cProfile, 10,000 `worker.ping` at once) shows Telesthete's `channel.send_message` called 10,012 times but `channel._transmit` only 1,278 times, and the hub idle in `epoll` for 11 of 24 s: the messages wait in the channel's send queue (`DEFAULT_WINDOW = 32` frames, `DEFAULT_RTO = 0.5` s in `telesthete/protocol/channel.py`) rather than in the hub's own code. Calls that never reach a worker are fine at the same size: 20,000 `secret.list` (a hub cap) at once all answered, in 10.8 s, slowest 7.8 s.
- The first 20,000 run also had 3,100 requests whose HTTP exchange failed in the client (`ClientOSError`); I did not record the message, and a second run did not repeat it.

The giving up is Telesthete's transport retry, which Bake has called housekeeping (18:45 CT, 17 November). What core does with it is the finding: a burst it cannot move is lost whole, and each call's answer says the worker disconnected although the worker never saw it. That is in "Faults found".

## Round 2: terminal storm

`tests/stress/terminals.py`. One worker.

- **25 terminals for five minutes.** All 25 opened in 0.13 s. Each, every 100 ms, typed `echo x$((n+0))` and read what came back: 74,400 numbered echoes typed, 74,400 came back, each terminal's in order, none missing. 148,850 door requests, every one `ok`, the slowest 0.058 s. Over the five minutes the hub's RSS went 52 → 56 MB, the worker's 38 → 39 MB; CPU 84 s for the hub, 31 s for the worker.
- **100 opened and closed, ten times.** 2,000 requests, all `ok`, slowest 0.051 s, 5.75 s in all. After each cycle the worker held 145 to 150 file descriptors (the 100 terminals' as they close) and 7 again five seconds after the last; `stream list open` lists none. Nothing left behind.
- **Past the brief: 1,000 terminals at once**, one echo typed into each and read back, then all closed: all 1,000 opened and echoed, 5,014 requests all `ok`, slowest 0.86 s, 6.8 s in all. The worker held 2,007 descriptors while they were open and 7 after.

## Round 3: stream volume

`tests/stress/streams.py`. Process streams (`shell.open` with `pty: false`), read through the door from position 0 to the end at the door's default page of 200 entries, `wait: 40`.

- **100,000 lines** (`seq 1 100000`): 9 entries in 10 reads (the worker sends what the pipe gives it, up to 64 KB an entry), all 100,000 lines, in order, 0.09 s.
- **One 50 MB write** (`sys.stdout.buffer.write(b'a' * 50_000_000)`): 763 entries in 764 reads, 6.9 s; all 50,000,000 bytes, unchanged. The worker's RSS went from 39 to 97 MB and stayed there.
- **Ten streams at once**, 10,000 numbered lines each, over three workers: all ten whole and in order, 0.17 s.
- **Past the brief: 100 at once**, 10,000 lines each: all 100 whole and in order, 2.2 s, slowest read 0.69 s.

No order problem, no drop, no error at any of these sizes.

## Round 4: large results

`tests/stress/results.py`. One worker. "1 MB" is 1,000,000 bytes. Text: a command printing that many `a`s, and `file.read` of such a file. Bytes: `file.read` with `encoding: base64` of that many random bytes, and `shell.exec` printing them.

| Size | Text, `shell.exec` | Text, `file.read` | Bytes, `file.read` base64 | Bytes, `shell.exec` |
|---|---|---|---|---|
| 1 MB | ok, whole, 0.12 s | ok, whole, 0.11 s | ok, whole, 0.19 s (1.33 MB of base64) | ok, 0.32 s: 947,000 characters, 3.75 MB of JSON |
| 10 MB | ok, whole, 1.04 s | ok, whole, 1.05 s | ok, whole, 2.08 s (13.3 MB) | `cap_failed`: 23,668,663 bytes encoded |
| 15 MB | ok, whole, 1.55 s | ok, whole, 1.53 s | `cap_failed`: 20,000,117 bytes | `cap_failed`: 35,521,137 bytes |
| 20 MB | `cap_failed`: 20,000,111 bytes | `cap_failed`: 20,000,116 bytes | `cap_failed`: 26,666,785 bytes | `cap_failed`: 47,355,057 bytes |

Over the 16,000,000-byte message size the call is refused quickly (0.05 to 0.4 s) and the hub stays up. The refusal reads, for example: `cap_failed: shell.exec failed: its result is 20000111 bytes encoded, over the 16000000 a message may be Next: Check the arguments against the schema of shell.exec.` The size is said; the next step is not one that helps (it is the same for every `cap_failed`), and the sentence has no full stop before "Next".

Bytes through `shell.exec` are not kept: its output is decoded as UTF-8 with `errors="replace"` (`defaults/shell/worker.py:115`), so random bytes come back as U+FFFD characters, each six bytes once escaped in JSON. 1 MB of random output is 3.75 MB on the wire, and 10 MB of it is already over the message size. `file.read` with base64 is the way to move bytes, up to about 12 MB.

The worker's RSS grew with the largest result it had sent and did not come down: 38 → 171 MB by the end. The hub's came back down to 84 MB.

## Round 5: churn

`tests/stress/churn.py`.

- **A worker linked and dropped 500 times.** The worker process was killed with SIGKILL as soon as it said it had linked, and started again: 500 times in 74.6 s. Every start linked at once as the same worker (slowest 0.16 s), with no second knock. Afterwards the hub lists one worker, a call to it answers, and the hub's RSS went 52 → 55 MB with 12 to 13 descriptors throughout.
- **Three workers heartbeating every 5 ms for ten minutes.** The hub has no way yet to set a worker's interval (H2 is not built; parity row 59), so these three are Python workers run in the round's own process, woken to heartbeat every 5 ms through the event a change of state uses. They managed one every 5.6 ms: 107,177 heartbeats each, 178 a second, 321,532 in all. A `worker.ping` to each every 10 s: 180 calls, all `ok`, slowest 0.031 s; all three stayed online. The hub's RSS went 55 → 58 MB, its CPU about 57 s a minute (one core). **The hub's data directory went from 4.9 MB to 3,319 MB**, steadily, about 330 MB a minute. A shorter run (16,245 heartbeats, then the database read directly) shows why: the hub keeps every heartbeat raw (H5, "delete nothing"), and each row is about 9,530 bytes, of which 9,319 are the worker's whole list of caps with their schemas, sent again in every heartbeat. At the normal 15 s interval that is about 55 MB a day for each worker, kept for ever. See "Faults found".
- **50 fresh keys knocking at once.** 50 new Python workers knocked at once; a loop listing `knocking` and calling `worker.approve` approved all 50 in 0.2 s (50 `ok`), and all 50 were listed online 1.75 s after they started. They stopped with no error.

## Round 6: hub restart under load

`tests/stress/restart.py`. Three workers. Throughout: `shell.exec` calls at 50 a second (the flood's commands) and 25 terminals each typed into and read every 100 ms. 30 s of load, then SIGTERM to the hub and a start again on the same data and port; 30 s; then SIGKILL and a start again; 30 s. Run twice; the second run's numbers, which measure "back" by each worker saying it linked again, are these.

| | Clean stop (SIGTERM) | kill -9 |
|---|---|---|
| Hub gone after the signal | 0.18 s, exit code 0 | 0.01 s |
| New hub listening | 0.20 s later | 0.21 s later |
| `workers` lists all three online, caps and all | at once (0.01 s) | at once |
| All three linked again (by themselves) | 15.5 s after the new hub listened | 15.9 s after |
| Calls in that time | 771 `worker_offline`, 20 no answer (hub down), 3 ok | 760 `worker_offline`, 11 no answer, 35 ok |
| Terminal writes in that time | 3,755 `worker_offline`; reads `ok` | 3,625 `worker_offline`; reads `ok` |

- **What came back by itself:** everything. Each worker's first attempt after the stop got no answer on `/band` for about 15 s (`Cannot link to the hub: no answer on ws://…/band. Trying again.`), and its next attempt half a second later linked. All 25 terminals were typed into and read again after each restart, with no new `shell.open`: the streams outlived both restarts. After the second restart every call was `ok` again.
- **What clients saw:** the requests in flight when the hub stopped got no HTTP answer (`ServerDisconnectedError`, `ClientOSError`), and requests while it was down could not connect (`ClientConnectorError`). For 15 s after the new hub listened, `workers` said every worker was online while every call to them answered `worker_offline` (H3 counts a worker online until three heartbeats are missed; the hub had just heard from them). An agent that checks `workers` first is told to go ahead and then refused.
- **What the journal holds** (all 6,148 entries since the round began): 4,537 call results, 1,531 `worker_offline`, 25 `stream_open`, 7 `hub_restarted` (the calls in flight at the stops, ended by the next start as it says it does), 41 events, 7 stream entries. No call is left `running`.
- **Once, in the first run**, a terminal write that was in flight as the clean stop closed the worker's link raised `EOFError` out of the door (`door.py:200` → `streams.py:310` → `link.py:348`), logged as `Error handling request`, and the client got HTTP 500 with a body that is not JSON-RPC. The second run did not repeat it.
- RSS: the hub's before each stop 54 to 56 MB, the new hub's 53 MB; the workers' did not move.

## Round 7: long haul

`tests/stress/longhaul.py`. One worker, one hour (22:57 to 23:57 CT, 17 November): `shell.exec` at 20 calls a second (the flood's commands), five terminals each typed into and read every 100 ms, and one process stream printing a line every 100 ms, followed with `wait: 40` reads.

| Minute | Calls (all `ok`) | Slowest call | Hub RSS | Worker RSS | Hub fds | Hub data |
|---|---|---|---|---|---|---|
| before | | | 51.9 MB | 38.5 MB | 13 | 1.7 MB |
| 1 | 1,200 | 0.112 s | 54.4 MB | 38.6 MB | 20 | 6.5 MB |
| 10 | 1,200 | 0.112 s | 55.4 MB | 38.6 MB | 20 | 23.7 MB |
| 20 | 1,200 | 0.111 s | 56.2 MB | 38.7 MB | 20 | 42.9 MB |
| 30 | 1,200 | 0.111 s | 57.0 MB | 38.8 MB | 20 | 62.2 MB |
| 40 | 1,200 | 0.113 s | 57.8 MB | 38.9 MB | 20 | 81.6 MB |
| 50 | 1,200 | 0.114 s | 58.0 MB | 38.9 MB | 20 | 100.9 MB |
| 60 | 1,200 | 0.113 s | 58.2 MB | 38.9 MB | 20 | 120.2 MB |

(Every minute is in the round's log; these are every tenth.)

- **Calls:** 72,000, every one `ok`; the slowest of any minute 0.114 s (a `sleep 0.1` command). None was followed as `running`.
- **Terminals:** 35,700 echoes typed into each, all 178,500 came back, each terminal's in order; 357,010 requests all `ok`, slowest 0.124 s.
- **Stream:** 35,493 lines, numbered from 0 with none missing and in order; 35,493 reads all `ok`.
- **Memory:** the hub's RSS rose 2.6 MB in the first half hour and 1.2 MB in the second (57.0 → 58.2 MB), 58.0 MB from minute 45 to 55: slowing, not shown to stop within the hour. The worker's moved 0.4 MB. File descriptors and threads did not change. CPU: the hub 7.2 s a minute, the worker 3.8 s.
- **Disk:** the hub's data grew steadily by about 1.9 MB a minute, 120 MB in the hour: the journal keeps every call with its result, the stream's recording is kept, and heartbeats are kept (round 5), none of it deleted (L1, S5, H5). At this load that is about 2.8 GB a day.

## Round 8: journal and records at size

`tests/stress/size.py`. One worker. The journal was filled to 100,000 entries through the door (90 % `secret.list` on the hub, 10 % `worker.ping`, each with its own why, `Stress fill <n>`): 48 s. Records exist only through plugin code (K2), so the round installs its own plugin, `tests/stress/plugin` (type `stress`, caps `stress.fill` and `stress.search`), on the round's hub only, through `plugin.install`; ten `stress.fill` calls made 50,000 records in 9.4 s. `hub.db` was then 63 MB.

While each read ran, a probe sent an MCP `ping` every 50 ms; "others wait" is its slowest answer, which is how long the hub answered nobody else.

| Read | Took | Others wait | Gave |
|---|---|---|---|
| door `journal`, default limit (50) | 0.003 s | 0.001 s | 50 entries |
| door `journal`, limit 1,000 | 0.033 s | 0.028 s | 1,000, 0.3 MB |
| door `journal`, limit 100,000 | 2.11 s | 1.40 s | 100,000, 29.7 MB |
| door `journal`, limit 1,000,000 (all) | 2.04 s | 2.04 s | 100,010, 29.7 MB |
| door `journal`, `text` of one why | 0.085 s | 0.072 s | 1 |
| door `journal`, `text` found nowhere | 0.077 s | 0.040 s | 0 |
| door `journal`, `cap: worker.ping`, limit 100,000 | 0.25 s | 0.25 s | 10,000, 3.3 MB |
| door `journal`, `worker`, limit 100,000 | 0.25 s | 0.25 s | 10,002, 3.3 MB |
| Data page `GET /api/journal` | 0.002 s | 0.001 s | 50 |
| Data page `GET /api/journal?limit=100000` | 1.64 s | 1.64 s | 100,000, 29.7 MB |
| Data page `GET /api/journal?text=…` (one why) | 0.081 s | 0.077 s | 1 |
| Data page `GET /api/journal/choices` | 0.001 s | 0.001 s | |
| `stress.search` one word, default 50 | 0.027 s | 0.023 s | 50 |
| `stress.search` one word, limit 50,000 | 0.12 s | 0.12 s | 13,550 |
| `stress.search` the whole type, limit 50,000 | 0.34 s | 0.34 s | 50,000 |
| `stress.search` one record's word | 0.002 s | 0.001 s | 1 |
| Data page `GET /api/records` | 0.075 s | 0.070 s | 50 |
| Data page `GET /api/records?type=stress&limit=50000` | 1.21 s | 0.94 s | 50,000, 24.3 MB |
| Data page `GET /api/records?text=amber&limit=50000` | 0.36 s | 0.28 s | 13,550, 6.6 MB |
| Data page `GET /api/record-types` | 0.005 s | 0.001 s | 3 types |

Everything answered, the right number of entries each time. A search for text scans the journal (`instr` over why, cap and result) and still takes under 0.1 s at 100,000 entries; the records' full-text index answers one word in 0.002 s. The cost is in the big reads: the hub reads the database on its one thread, under its lock, so while it reads and encodes the whole journal (2 s here) it answers no one else, not the door, not the site and not the workers' links. That grows with the journal, which is never trimmed (L1); at round 7's 20 calls a second it gains 1.7 million entries a day. The hub's RSS rose to 235 MB while it built the 30 MB replies and was 178 MB at the end.

## Forever-waits found

None in these rounds. Every request was answered or failed at the transport, with nothing on the client to cut it short; the slowest answer was 122 s (round 1, 20,000 calls at once, answered `worker_disconnected`).

The forever waits found before (`docs/parity.md`, "Found waits": a command that never ends cannot be stopped; a frozen worker holds its calls) were not looked for again here.

## Faults found

One line each, with the steps to see it again. None is fixed here.

1. **About 10,000 calls in flight at once to workers are lost whole, answered `worker_disconnected` although no worker got them.** Start a hub and one worker; send 10,000 `call worker=<it> cap=worker.ping` at once through the door: about 4 are answered `ok`, the rest `worker_disconnected` after about 13 s, when Telesthete tears the link down (`peer unresponsive after 10 retransmits`). 6,000 at once all succeed. `tests/stress/flood.py` shows it in its last step (20,000 `shell.exec` at once over three workers).
2. **The heartbeat history grows about 9.5 KB per heartbeat, almost all of it the caps list repeated.** Start a hub and a worker; after N heartbeats, `SELECT avg(length(body)) FROM heartbeats` in `hub.db` is about 9,530, of which the `caps` field is 9,319. At 15 s that is about 55 MB a day per worker with nothing deleted (H5). Whether to keep caps in each stored heartbeat, or keep it all, is Bake's: see Needs Bake in the pull request.
3. **After a hub restart, `workers` lists workers online for up to 15 s while every call to them answers `worker_offline`.** Run `tests/stress/restart.py`, or by hand: hub and worker linked; restart the hub; at once, `workers` says `online: true` and `call worker=<it>` answers `worker_offline` until the worker says `Linked to …` again, 15 s later.
4. **A worker takes 15 s to link again after the hub restarts**, because its first attempt waits about 15 s for an answer on `/band` from the new hub before trying again. Same steps as 3; the worker's lines show `The link to the hub was lost; linking again.`, then 15 s later `Cannot link to the hub: no answer on ws://…/band. Trying again.`, then `Linked` 0.5 s after.
5. **A `stream write` whose link closes mid-write answers HTTP 500, not JSON-RPC**, and logs a traceback (`EOFError` from `link.py:348`, unhandled in `streams.py:310`). Seen once in two runs of `tests/stress/restart.py`: 25 terminals typed into while the hub stops cleanly.
6. **An over-size result's refusal points at the schema**, and runs into "Next" without a full stop: `…over the 16000000 a message may be Next: Check the arguments against the schema of shell.exec.` Steps: `call cap=shell.exec args={"cmd": "head -c 20000000 /dev/zero | tr '\\0' a"}`.
7. **A read of the whole journal stops the hub for as long as it takes**: 2.0 s at 100,000 entries, during which no request and no link is served. Steps: `tests/stress/size.py`; or fill a hub's journal and call `journal limit=1000000` while pinging the door.
8. **The worker keeps the memory of the largest result it sent**: 38 → 171 MB after round 4, 39 → 97 MB after one 50 MB stream, and not given back. Steps: `tests/stress/results.py`, then read the worker's `VmRSS`.

## Did not run

- **rookbeta.bake.systems**: not to be loaded (the brief).
- **Workers other than Python**: core has only the Python worker.
- **Heartbeats at 5 ms set by the hub**: the hub cannot set a worker's interval (H2 not built), so the three fast workers were woken by the test itself; see round 5.
- **A second machine**: everything ran on cachyrig, over loopback, so network loss and delay were not part of any round.
