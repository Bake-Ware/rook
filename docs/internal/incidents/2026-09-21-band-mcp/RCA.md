# RCA: recurring rook-band-mcp memory exhaustion

Status: source fixes deployed; 30-minute memory acceptance completed. Latency
acceptance remains OPEN: quiet p95 was 292.4 ms versus 198.0 ms before; see
FIX.md for full-window and controlled-comparison measurements.

## Summary and impact

The bridge allowed session and call state to outlive its useful lifetime without
sufficient cleanup or bounds. Stateful MCP sessions abandoned without DELETE had
no idle deadline. In production MCP SDK 1.27.1, even DELETE could leave a transport
in the session manager's registry. Failed transport sends leaked pending reply
futures. Separately, console output without a newline could grow an in-memory
partial line indefinitely. These ownership defects are reproduced locally and
fixed at their source. No historical live heap was captured, so the relative
contribution of each defect to the five OOM events cannot be quantified.

The bridge repeatedly consumed most of a 954 MB VM's memory. MCP clients lost
management access; host-wide thrashing delayed journald and SSH. The incident
report records four kills with approximately 543–558 MB anonymous RSS; Bake
reported a fifth at 07:22 UTC and about 424 MB reached in under four hours.
The hub and rook-remote were not restarted by this repair.

## Timeline (UTC, 2026 unless noted)

| Time | Event / evidence |
|---|---|
| Sep 18 16:50:45 | rook-remote began its still-running instance. |
| Sep 19 05:08 | First OOM, about 548 MB anonymous RSS (INCIDENT.md). |
| Sep 19 15:42 | Second OOM, about 558 MB anonymous RSS. |
| Sep 20 04:47 | Third OOM, about 543 MB anonymous RSS. |
| Sep 20 22:48 / 23:12 | Journald watchdog timeouts during host thrashing. |
| Sep 21 00:19–03:13 | sshd MaxStartups throttling; 49 dropped connections recorded. |
| Sep 21 03:13:04 | Fourth OOM, about 545 MB anonymous RSS. |
| Sep 21 03:13:13 | Bridge restarted; historical NRestarts=4. Band worker also restarted around this event. |
| Sep 21 about 03:21 | Fresh bridge reported around 84 MB RSS. |
| Sep 21 03:25–03:33 | Attempt 1 identified session and send-failure retention, confirmed live OAuth patch, stopped because required sparky SSH fallback was unavailable. No fix deployed. |
| Sep 21 07:22 | Fifth OOM (Bake's update); roughly 424 MB reached in under four hours, indicating load-dependent acceleration. |
| Sep 21 about 11:20 | Claude installed containment: 450 MiB MemoryMax, Restart=always, RestartSec=2, OOMScoreAdjust=500, 1 GiB swap, swappiness=10. Verified and preserved in attempt 2. |
| Sep 21 attempt 2 | OAuth sync committed first as f71d95d; fixes committed separately on cachyrig master. No stale worktree/origin source used. |
| Sep 21 11:25:21–11:27:21 | Before probe: 115 real HTTP/tool calls, zero errors; bridge RSS 104,972→108,952 KiB. |
| Sep 21 11:32:51 | First ten-minute rollback armed; detached bridge restart scheduled. |
| Sep 21 11:32:55 | Session/call fix 0c1e951 active. First reconnect check succeeded; 28 workers and real call verified. Rollback canceled after verification. |
| Sep 21 11:34:08 | Initial soak began. Historical journal query timed out; simultaneous host pressure/swap confounded this short interval. It is not the final acceptance run. |
| Sep 21 11:38:45 | Fresh dead-man timer armed for console buffer fix; initial probe stopped intentionally. |
| Sep 21 11:38:49 | Final code 0852284 active; bridge PID 155846. Hub PID 725 and rook-remote PID 113646 unchanged. |
| Sep 21 11:40:02 | Final rollback timer canceled after workers and round trip succeeded; fresh 30-minute probe started. |
| Sep 21 11:56:09 | One 10-second probe worker-call timeout during a diagnostic journal-query/host-pressure interval. Bridge PID remained unchanged; this sample is retained, not excluded. |
| Sep 21 about 11:58–12:02 | Completed isolated, randomized old/fixed lifecycle A/B with 100 real worker calls per variant and zero errors. |
| Sep 21 12:10:04 | Final soak completed: 1,800.29 s, 1,759 calls, one timeout, unchanged PID, peak HWM 98.46 MiB; last-ten-minute RSS+swap 88.81–90.16 MiB. |
| Sep 21 12:10:53–12:12:53 | Quiet repeat: 120/120 calls, unchanged RSS+swap 92,020 KiB; real-call p50/p95 21.258/292.432 ms. Strict p95 acceptance remains open. |

## Exact mechanisms and fixes

References below use source in commit `0852284` unless stated otherwise.

1. **Unbounded MCP ownership.** Old `server.py:95` relied on FastMCP's default
   stateful session manager. SDK 1.27.1 `_handle_stateful_request` registered each
   transport in `_server_instances` and retained a session task in its task
   group. No idle timeout was configured. Its runner's `finally` excluded
   already-terminated transports from registry cleanup, so DELETE did not
   reliably remove the transport root. The new
   `rook/band_mcp/http_sessions.py:63` owns task lifetime and unconditionally
   removes transport and owner entries in `finally`. Five-minute inactivity
   expires abandoned sessions. Active POSTs suspend idle expiry; expiry returns
   404 on later use, allowing reinitialization. Admission caps are 128 sessions
   and 256 HTTP requests; overload returns 503/Retry-After without evicting an
   active call. SDK wire handling and stateful compatibility remain in use.
2. **Failed-send futures.** Old `BandClient.call` registered `_pending[mid]`, then
   awaited `transport.send` before entering its cleanup `try/finally`. Exceptions,
   cancellation or serialization failures bypassed the pop. The fixed
   `rook/band_mcp/client.py:153` encloses serialization, send and reply in one
   timeout/cleanup lifetime, cancels unfinished futures and caps pending calls at
   512. Normal reply timeouts already cleaned up on old code; they were not all
   leaks. The timeout now also bounds a stalled send.
3. **SSE send failure/disconnect.** SDK SSE handling can swallow a send exception.
   `http_sessions.py:95` cancels the owner directly at the failing send boundary,
   in addition to outer exception/cancellation cleanup. A normally abandoned
   session is bounded by its idle TTL rather than an eternal disconnected task.
4. **Unterminated console output.** Old `ConsoleStore.append` concatenated each
   chunk into `_pending[room]` until a newline appeared; `MAX_LINE` applied only
   after complete lines reached persistence. `console_rooms.py:189` now writes
   complete 4,000-character pieces and retains only the shorter tail. The
   regression sends 819,200 characters without a newline and checks both the
   bound and exact recovered output.
5. **Secondary bounded state.** OAuth codes retain their five-minute expiry but
   now cap at 1,024 records and 4,096 characters per query field; expired records
   are swept on authorization and token requests. Admin sessions are swept on
   login and capped at 256. These are preventive bounds; historical OOM
   attribution to OAuth/admin sessions is not established.

MCP streams are rendezvous streams in the installed SDK; no replay event store
is configured. Chat/console reads remain paginated and database-backed. The
repair does not replace them with transcript caches.

## Reproduction and evidence

The local old-code control uses the production MCP 1.27.1, Starlette 1.0.0 and
Uvicorn 0.47.0. Local Python is 3.14; production is 3.12. AnyIO was aligned to
4.13.0 after the first allocation run; the first before allocation artifact
used 4.15.1, a disclosed difference; the final matched before/after evidence
uses 4.13.0 for both. No dependency upgrades were deployed.

After 200 mixed sessions / 2,400 calls, old code retained 200 transports,
100 live ServerSession objects, 200 failed-send futures and 302 tasks. After
accelerated idle expiry, fixed code retained zero transports, zero server
sessions, zero pending futures and only two harness tasks. Tracemalloc and
objgraph counts identify registry/task/future ownership; allocation sites show
retained asyncio locks, AnyIO tasks/streams and MCP/Pydantic session state.
Actual objgraph back-reference chains confirm `manager → dict → transport`
and `BandClient → dict → Future`. No object values, live heap dumps or
credentials are present in the evidence. A default five-minute-TTL stress run
completed 4,992 calls, exercised the 128-session admission cap and expiry, and
settled near 107 MiB RSS with tracing enabled.

A separate 5,000-call single-session test plateaus on both versions, supporting
session churn and failure-path retention rather than a claim that every
successful round trip leaks. Final regression suite: 72 passed. The old-code
control fails DELETE, abandoned-session expiry and failed/cancelled/serialization
send cleanup. Console-bound regression also verifies lossless persistence.

## Why detection was late

Existing tests exercised successful request/reply and console functionality,
without sustained abandoned-session churn, failed-send ownership assertions or
long output lacking newline delimiters. Different local and production SDK
versions concealed lifecycle differences. Restart recovery masked retained
ownership, and no effective alert evidence was supplied for rising bridge RSS
or restart counts before the incident. This does not establish that no
monitoring existed anywhere.

## Contributing factors

- A 954 MB shared host had neither swap nor a bridge memory ceiling before
  containment; bridge growth became host-wide thrashing.
- SDK-default session lifecycle was trusted without service-level retention
  limits. Client abandonment is normal under reconnects and cannot depend on
  graceful DELETE for reclamation.
- Live OAuth source differed from clean Git state; attempt 2 committed the
  working production change before repair to prevent its loss.
- SSH scanner pressure and password authentication increased incidental load.
- sparky was the sole documented SSH fallback and was offline, leaving bridge
  management dependent on the service being repaired.

## Corrective actions and follow-ups

Completed: source fixes and regression harness; separate OAuth/fix commits;
bridge-only release staging; hash verification; on-host code backups;
independent systemd rollback timers; real worker checks; containment preserved.
Final measurements and acceptance outcome are in FIX.md.

| Follow-up | Owner / completion criterion |
|---|---|
| Tail-latency acceptance | Maintainers/operations: obtain a larger stable baseline and isolate host scheduling/I/O tails. Quiet p95 remains above the original short baseline; controlled A/B does not certify strict equivalence. No unrelated service tuning performed. |
| RSS/restart alerting | Bake/operations: alert at 250 MiB sustained RSS or growing RSS+swap, urgent at 350 MiB, and on any unexpected bridge restart. Track latency, errors, swap and memory PSI. Route alerts through an independent path. Not installed in this change. |
| sshd password auth | Bake/operations: verify independent key login first, then disable password authentication and confirm access. No sshd change made here. |
| rook-remote secrets in argv | Bake/operations: move secrets to protected credentials/EnvironmentFile support; rotate web password and affected PSKs through coordinated band rotation. Values remain <redacted>. No remote-service change made here. |
| Independent access | Bake/operations: restore sparky and add a tested second SSH path; verify OCI serial-console access and required IAM permissions. Do not rely on Rook to reach the only recovery host. |
| Hermes cron model/provider | Bake/agent owners: set an explicit model and provider on each Hermes cron job; test one controlled run and failure alert. Defaults can change or resolve incorrectly. Jobs not edited here. |
| SDK upgrades | Maintainers: pin/test a production dependency set and run session expiry, DELETE, cancellation and overload regressions before upgrading. Adapter uses the internal FastMCP manager slot. |
| Longer observation | Operations: retain daily RSS+swap/restart trends under real peak traffic; 30 minutes is the minimum acceptance window, not proof for all future workloads. |
