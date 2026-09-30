"""Permissions end to end on an isolated test hub (opt-in): the hub signs a
ticket for each call, workers learn the op key from the hub's granted
announce and verify tickets over the real relay (audit mode: nothing is
refused, the result lands in the worker's audit log)."""

from __future__ import annotations

import time


def test_hub_status_and_worker_verifies_tickets(hub):
    status = hub.call("rook_call", cap="policy.status", worker="rook")
    assert status["ok"], status
    assert status["result"]["mode"] == "audit"
    if not status["result"]["tickets"]:
        import pytest
        pytest.skip("this test hub has no root signing key (tickets disabled)")
    worker = hub.workers[0]
    deadline = time.time() + 45          # the hub re-announces its grant every ~30 s
    entry = None
    while time.time() < deadline:
        ping = hub.call("rook_call", cap="info.ping", worker=worker)
        assert ping["ok"], ping
        audit = hub.call("rook_call", cap="log.audit", worker=worker,
                         args={"limit": 5, "cap_prefix": "info.ping"})
        rows = [r for r in ((audit.get("result") or {}).get("entries") or [])
                if isinstance(r, dict)]
        entry = rows[-1] if rows else None
        if entry and (entry.get("ticket") or {}).get("verified"):
            break
        time.sleep(3)
    assert entry, "no audit row for info.ping"
    assert entry["ticket"]["verified"], entry
    assert entry["ticket"]["kid"] == status["result"]["op_kid"]
    assert entry["decision"] == "allow"


def test_policy_explain_through_mcp(hub):
    worker = hub.workers[0]
    why = hub.call("rook_call", cap="policy.explain", worker="rook",
                   args={"principal": "integration:telegram", "cap": "shell.exec",
                         "worker": worker})
    assert why["ok"], why
    assert why["result"]["decision"] == "would_deny" and why["result"]["tier"] == "exec"
