"""The soak runner's report math (tests/soak/soak.py); the soak itself needs
the live sidecar and runs by hand (docs/castling-soak.md)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location("soak", Path(__file__).parent / "soak" / "soak.py")
soak = importlib.util.module_from_spec(spec)
spec.loader.exec_module(soak)


def test_percentiles_are_nearest_rank():
    ms = list(range(1, 101))
    assert (soak.pct(ms, 50), soak.pct(ms, 95), soak.pct(ms, 99), soak.pct(ms, 100)) == (50, 95, 99, 100)
    assert soak.pct([7], 99) == 7


def test_latencies_count_failures_per_op():
    calls = [{"op": "ping", "ok": True, "ms": 2}, {"op": "ping", "ok": False, "ms": 9},
             {"op": "exec", "ok": True, "ms": 5}]
    lat = soak.latencies(calls)
    assert lat["ping"]["n"] == 2 and lat["ping"]["fail"] == 1 and lat["ping"]["max"] == 9
    assert "| exec | 1 | 0 |" in soak.table(lat)


def test_recovery_is_the_first_quick_success_sent_after_the_disruption():
    calls = [{"op": "ping", "t": 100.0, "ok": True, "ms": 5},          # before it
             {"op": "ping", "t": 121.0, "ok": True, "ms": 20000},      # stuck in flight, retried
             {"op": "ping", "t": 125.0, "ok": False, "ms": 20000},     # lost
             {"op": "mcp_ping", "t": 108.0, "ok": True, "ms": 6},      # back
             {"op": "view_caps", "t": 104.0, "ok": True, "ms": 1}]     # not a worker call
    back, hurt = soak.recovery(calls, 101.0)
    assert round(back, 3) == 7.0 and hurt == 2


def test_segments_split_at_a_new_pid(tmp_path):
    samples = [{"t": 1, "hub": {"pid": 1, "rss_kb": 10}}, {"t": 2, "hub": None},
               {"t": 3, "hub": {"pid": 2, "rss_kb": 5}}, {"t": 4, "hub": {"pid": 2, "rss_kb": 6}}]
    assert [[s["rss_kb"] for s in seg] for seg in soak.segments(samples, "hub")] == [[10], [5, 6]]


def test_report_reads_a_results_folder(tmp_path):
    (tmp_path / "calls.jsonl").write_text("\n".join(json.dumps(c) for c in [
        {"t": 10.0, "op": "ping", "who": "owner", "ok": True, "ms": 3},
        {"t": 70.0, "op": "ping", "who": "owner", "ok": False, "ms": 30, "err": "timeout"}]) + "\n")
    (tmp_path / "events.jsonl").write_text(json.dumps({"t": 50.0, "kind": "chaos", "what": "restart_hub",
                                                       "ok": True, "start": 40.0, "took": 3.0}) + "\n")
    proc = {"pid": 1, "rss_kb": 2048, "threads": 2, "fds": 7, "cpu_s": 1.0}
    (tmp_path / "samples.jsonl").write_text("\n".join(json.dumps(
        {"t": t, "calls": 0, "relay": proc, "hub": proc, "worker": proc, "journal_bytes": 1e6,
         "records_bytes": 0, "replay_bytes": 0, "replay_lines": 0}) for t in (10.0, 70.0)) + "\n")
    text = soak.report(tmp_path)
    assert "2 operations" in text and "- ping: timeout (1)" in text and "restart_hub" in text
    assert (tmp_path / "report.md").read_text() == text
