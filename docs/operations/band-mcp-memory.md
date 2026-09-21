# Bridge memory ownership

The bridge retains stateful MCP compatibility, with a five-minute idle session
TTL, at most 128 sessions and 256 concurrent HTTP requests. New requests over
capacity receive 503 with Retry-After; active sessions are not evicted to admit
new clients. Active POSTs suspend idle expiry until they finish. Expired IDs
receive 404; clients must initialize a new session. DELETE, send failure,
cancellation, expiry and shutdown release transport and session-task ownership.
SDK streams use rendezvous buffers and the bridge does not configure an event
replay store. The adapter is tested against MCP 1.27.1; re-run lifecycle tests
before SDK upgrades because it integrates with FastMCP's internal manager slot.

BandClient has at most 512 pending calls, and its timeout covers both sending
and awaiting a reply. Every exit releases/cancels the future. OAuth codes have
a five-minute TTL, 1,024-entry limit and 4,096-character per-field limit. Admin
sessions expire on login and are capped at 256. Persistent journal/chat/console
stores retain their existing paginated database-backed behavior.

Allocation diagnostics are opt-in: SIGUSR2 on the bridge PID toggles tracemalloc;
SIGUSR1 writes only the top 20 allocation sites/sizes/counts to its journal.
Tracing begins with new allocations, not the pre-existing heap. Never take or
publish live heap/core dumps: they contain credentials and user content.

Validation tools:

- `tools/diagnostics/band_mcp_memory.py`: synthetic ASGI/auth/session/tool/store
  churn, injected reply/timeout/send failure, allocation counts and RSS. Use
  `--idle 0.1 --settle 0.2` to accelerate expiry; production uses 300 seconds.
- `tools/diagnostics/band_mcp_probe.py`: on-host HTTP and real worker round trips,
  with one-minute RSS samples and per-call latency. Reads existing credentials
  into memory; writes scalars only to a newly created mode-0600 output file.
- `tests/test_band_mcp_memory.py`: lifecycle, capacity, send failure,
  cancellation, timeout, OAuth and admin retention regressions.

Production deployment and measured results are recorded in the incident's
FIX.md, RCA.md and RECOVERY.md under
`/home/bake/projects/incidents/2026-09-21-bakenetca-oom/`.
Keep the installed 450 MiB cap, Restart=always/RestartSec=2, OOMScoreAdjust=500,
1 GiB swap and swappiness=10. A restart below the cap is a failed soak.
