# bakenetcanada: 3am bridge recovery runbook

Memory repair is deployed and completed a 30-minute soak; strict p95 latency
acceptance remains open. See FIX.md for measurements.

Current bridge release: `/opt/rook-releases/bandmcp-20260921-0852284`.
Only operate on `rook-band-mcp.service` during bridge recovery. Hub, rook-remote,
nginx, SSH and workers have separate lifecycles. Do not paste raw process argv,
unit environment, tokens, passwords or PSKs. Use `<redacted>` in incident notes.

## 1. Recognize and confirm cheaply

Symptoms: Rook tools stall, return session errors or stop listing workers;
bridge RSS rises across samples; repeated bridge restarts; host pressure delays
SSH/journald. A restart restoring access is containment, not proof of repair.

Through Rook, use `rook_call(cap="shell.exec", worker="bakenetcanada",
args={"cmd": "..."}, timeout=20)`. The argument is `cmd`, not `command`.
Run these scalar checks first:

```sh
systemctl show rook-band-mcp.service -p ActiveState -p MainPID -p NRestarts -p WorkingDirectory -p MemoryCurrent -p MemoryPeak -p MemoryMax -p Restart
pid=$(systemctl show rook-band-mcp.service -p MainPID --value)
if [ "$pid" -gt 0 ]; then
  awk '/^(Name|VmRSS|VmHWM|VmSwap|Threads):/' /proc/"$pid"/status
fi
free -m
cat /proc/pressure/memory
uptime
```

RSS is `VmRSS`; process peak RSS is `VmHWM`. Cgroup MemoryCurrent/MemoryPeak
include different memory categories. A falling RSS accompanied by rising VmSwap
is not a healthy memory plateau. Compare RSS+swap as well. Check twice, one
minute apart. The installed MemoryMax is 471859200 bytes (450 MiB).

If needed, inspect a small journal tail **privately** and redact before sharing:

```sh
sudo -n timeout 5s journalctl -u rook-band-mcp.service --since '-5 minutes' -n 30 --no-pager
sudo -n timeout 5s journalctl -k --since '-5 minutes' -n 60 --no-pager
```

If the bounded journal command times out, skip it; do not broaden/retry the scan.
Avoid hours-long journal scans on this 1 GB host during pressure. In attempt 2,
a historical query timed out during a swap/latency spike. Avoid full `ps` or
unit/environment dumps: rook-remote's argv contains credentials.

## 2. Preserve heap evidence when there is headroom

The deployed bridge has safe allocation diagnostics. They log only file/line,
allocation bytes/counts and current/peak traced bytes; never dump objects,
locals, credentials, request bodies or the whole heap.

First check whether diagnostics are enabled:

```sh
pid=$(systemctl show rook-band-mcp.service -p MainPID --value)
sudo -n systemctl kill --kill-whom=main --signal=SIGUSR1 rook-band-mcp.service
sudo -n timeout 5s journalctl -u rook-band-mcp.service --since '-2 minutes' -n 25 --no-pager
```

If the log says tracing is off, enable it, let normal traffic run for 30–60
seconds, and take a snapshot. Record only allocation lines in incident evidence.

```sh
sudo -n systemctl kill --kill-whom=main --signal=SIGUSR2 rook-band-mcp.service  # toggles tracing ON only if currently off
# Wait 30–60 seconds while the service handles normal traffic.
sudo -n systemctl kill --kill-whom=main --signal=SIGUSR1 rook-band-mcp.service  # allocation summary to journal
sudo -n timeout 5s journalctl -u rook-band-mcp.service --since '-2 minutes' -n 25 --no-pager
sudo -n systemctl kill --kill-whom=main --signal=SIGUSR2 rook-band-mcp.service  # toggles tracing OFF after this capture
```

Tracing covers allocations made after it was enabled; it cannot reconstruct the
old heap or a dead process. It adds overhead, so leave it off for normal latency
measurement. Do not send these signals to the pre-fix release: it has no handler.
When close to the cap or under severe pressure, collect scalar status and
recover promptly instead of allocating a snapshot.

## 3. Recovery while Rook still works

For a same-code restart, schedule it detached so the management call can return:

```sh
sudo -n systemd-run --unit=bandmcp-recovery-$(date -u +%Y%m%dT%H%M%S) --collect --on-active=3 systemctl restart rook-band-mcp.service
```

Expect the MCP connection to drop. Retry workers and a cheap call every 30–60
seconds; do not flood reconnects. Before this fix Bake observed 4–7 minute client
reconnect delays. Both fix deployments succeeded at their first scheduled
reconnect check, within roughly a minute; this is an observed upper bound, not
a claim about every client's reconnect policy.

After reconnect, verify ActiveState, actual WorkingDirectory, new PID, RSS and
restart count, then `rook_workers` and this real round trip:

```json
{"cap":"shell.exec","worker":"bakenetcanada","args":{"cmd":"printf recovery-ok"},"timeout":20}
```

An HTTP 200 or active service alone is insufficient; require the worker's
successful reply. A 404 for an expired MCP session means reinitialize the client.
At session/request capacity, 503 includes Retry-After; respect it. Do not enlarge
the memory cap to make a leaking process look healthy.

## 4. Dead-man pattern for code/config changes

Use an on-host systemd timer independent of the bridge. Prepare the exact
rollback before selecting a new release. Keep all credentials and shared
SQLite state in place; do not copy them into an artifact or rollback old data.
Use unique unit names if a previous transient unit still exists.

1. Record current bridge WorkingDirectory and hashes. Copy active **code** to a
   private backup directory. Keep the previous versioned release untouched.
2. Stage and verify the replacement using the existing venv. Write a root-owned
   rollback script that restores only the bridge release-selection override,
   runs daemon-reload and restarts only rook-band-mcp. Validate with `sh -n`.
3. Arm the timer and verify it is active **before** writing the new override or
   scheduling a restart. The deployed recovery script below is a real path.

```sh
sudo -n systemd-run --unit=bandmcp-rollback --on-active=600 /bin/sh /opt/rook-band-mcp/rollback-20260921-1140/rollback.sh
systemctl is-active bandmcp-rollback.timer
# Select the verified new bridge release only after the timer is active.
sudo -n systemd-run --unit=bandmcp-restart --on-active=3 systemctl restart rook-band-mcp.service
```

4. Reconnect every 30–60 seconds. Verify service, exact release, workers, a real
   call and containment; compare hub/rook-remote PID/start times with before.
5. Only after those checks pass, disarm the timer:

```sh
sudo -n systemctl stop bandmcp-rollback.timer
systemctl is-active bandmcp-rollback.timer  # must report inactive
```

The final deployment used `bandmcp-rollback-final.timer`, which was disarmed
at 11:40 UTC after successful checks. Both deployment timers are inactive.
No automatic rollback occurred in this attempt.

If there is no reconnect within ten minutes, let the timer run. Stop deployment
work and report whether the old release recovered. If rollback cannot be
verified, use independent console access; do not keep changing releases blindly.
For a future release, update the script to restore that deployment's immediate
predecessor instead of blindly reusing a historical path.

## 5. Exact rollback commands for this deployment

Roll back only the console-buffer revision to the verified session/call fix
`0c1e951` (this restores the saved bridge-only override):

```sh
sudo -n systemd-run --unit=bandmcp-manual-rollback-$(date -u +%Y%m%dT%H%M%S) --collect --on-active=3 /bin/sh /opt/rook-band-mcp/rollback-20260921-1140/rollback.sh
```

Emergency rollback of the entire repair to the original OAuth-patched release
`/opt/rook-releases/voice-20260910-84f2e66`:

```sh
sudo -n systemd-run --unit=bandmcp-original-rollback-$(date -u +%Y%m%dT%H%M%S) --collect --on-active=3 /bin/sh /opt/rook-band-mcp/rollback-20260921-1132/rollback.sh
```

The latter removes only `zzz-memory-fix.conf`, exposing the unchanged existing
`90-enrollment-upgrade.conf`. Original code is also backed up under the 1132
backup directory. It reintroduces the known leak, so keep containment and
schedule forward repair. Both scripts preserve shared state and containment.
They were syntax-checked; the rollback execution itself was not needed/tested
by intentionally disrupting a healthy final deployment.

## 6. Bridge down and no SSH path

sparky is offline; do not wait for it or assume Rook-mediated SSH is independent.
Use the Oracle account through a separate browser/device:

1. Select **ca-montreal-1**, the correct compartment, then Compute → Instances →
   **bakenetcanada**. Confirm the instance identity and state from trusted
   inventory. Check metrics and capture console history if useful.
2. Open the instance's Console connection action and launch its Cloud Shell
   serial connection. This requires the appropriate IAM permissions. A local
   serial connection is an alternative with a public key and outbound SSH on
   port 443. Follow Oracle's [instance console instructions](https://docs.oracle.com/en-us/iaas/Content/Compute/References/serialconsole.htm).
3. A serial connection is access to the console, not automatic guest root login.
   Use the established guest recovery credentials/procedure. If login is
   unavailable, involve the tenancy/guest administrator for image-appropriate
   recovery. OCI account/serial access was not exercised during this repair.
4. Once in the guest, run the cheap checks above. Confirm bridge unit status,
   cap, swap, free disk, loopback listener 8765, and the selected release.
   Inspect the bridge's short journal tail. If the bridge is active but public
   access fails, inspect nginx/Cloudflare routing and auth errors read-only;
   do not restart unrelated services as a bridge fix.
5. Recover or roll back only the bridge using the commands above. If systemd
   cannot run or the guest is completely unresponsive, escalate for an
   explicitly authorized controlled reboot/recovery operation. A host reboot
   affects other services and is not this runbook's first action.

Useful guest checks:

```sh
df -h / /var
swapon --show --bytes
sysctl vm.swappiness
ss -ltn '( sport = :8765 )'
systemctl show rook-band-mcp -p MemoryMax -p Restart -p RestartUSec -p OOMScoreAdjust
```

If the console cannot be opened, verify IAM, region/compartment and console
connection state with the tenancy administrator. Consult Oracle's
[compute troubleshooting index](https://docs.oracle.com/en-us/iaas/Content/Compute/References/troubleshooting-compute-instances.htm).
Do not guess credentials, disable host-key verification, or open password SSH
as an improvised recovery method.

## 7. Prevention and escalation

Keep the existing controls unchanged:

- `/etc/systemd/system.control/rook-band-mcp.service.d/50-MemoryMax.conf`: 450 MiB.
- `/etc/systemd/system/rook-band-mcp.service.d/zz-memory-containment.conf`:
  Restart=always, RestartSec=2, OOMScoreAdjust=500.
- 1 GiB `/swapfile`, persistent in fstab; swappiness=10 in
  `/etc/sysctl.d/90-swappiness.conf`.

Alert on unexpected restarts, rising RSS+swap and latency/errors; warn at 250 MiB
sustained RSS and urgently investigate at 350 MiB, well below the cap. Any cap
approach or restart is an acceptance failure. Verify a second independent SSH
path and rehearse serial-console access. Track sshd hardening, secrets migration
and rotation, explicit Hermes cron model/provider, and SDK lifecycle regressions
as the separate follow-ups listed in RCA.md.

Escalate with UTC times, release/PID, RSS/HWM/swap, restart count, worker-call
result and redacted errors. If a design decision is needed or a failed deploy
has not cleanly rolled back, stop changes and report the blocker.
