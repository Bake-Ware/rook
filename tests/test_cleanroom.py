"""The clean-room harness (tools/cleanroom): its matrix, reports and toy repo.

The runs themselves need disposable VMs and an emulator, so they are not part
of the test suite; these tests keep the data and the bookkeeping honest.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from tools.cleanroom import report, scenarios, toy_seed
from tools.cleanroom.__main__ import load_matrix, validate
from tools.cleanroom.providers import NAME_PREFIX, TAG, ProxmoxProvider, ProviderError


def test_matrix_is_valid_and_every_scenario_is_implemented():
    m = load_matrix()
    assert validate(m) == []
    assert {"bachelor", "homelab"} <= set(m["hardware"])
    assert {"claude-subscription", "local-only", "mixed"} <= set(m["models"])
    assert {"locked-down", "balanced", "yolo"} <= set(m["trust"])
    order = [name for name, _ in scenarios.ORDER]
    assert [s["id"] for s in m["scenarios"]] and all(s["id"] in order for s in m["scenarios"])


def test_validate_catches_broken_profiles():
    m = load_matrix()
    m["hardware"]["two-hubs"] = {"machines": [
        {"role": "a", "memory_mb": 1, "cores": 1, "hub": True, "worker": True},
        {"role": "b", "memory_mb": 1, "cores": 1, "hub": True}]}
    m["models"]["odd"] = {"house_agent": {"backend": "carrier-pigeon"}}
    m["trust"]["weird"] = {"default_level": "whatever"}
    errs = "\n".join(validate(m))
    assert "two-hubs" in errs and "carrier-pigeon" in errs and "whatever" in errs


def test_matrix_carries_no_site_details():
    text = open(os.path.join(os.path.dirname(scenarios.__file__), "matrix.json")).read()
    for needle in ("192.168.", "bake", "kaiju", "soundwave", "cachyrig"):
        assert needle not in text.lower()


def test_run_report_counts_steps_touches_and_tokens(tmp_path):
    run = report.Run(run_id="t1", hardware="bachelor", model="claude-subscription", trust="balanced")
    with run.step("onboard-hub", "install") as s:
        s.touch("paste the installer")
        s.tokens(input_tokens=10, output_tokens=5)
    with run.step("pair-phone", "pair") as s:
        s.touch("type the code", 2)
        s.fail("refused")
    with run.step("voice", "first voice") as s:
        s.skip("no voice server")
    with run.step("first-request", "ask") as s:
        raise RuntimeError("boom")          # a crash fails the step, the run goes on
    run.mark_first_useful()
    t = run.totals()
    assert (t["passed"], t["failed"], t["skipped"]) == (1, 2, 1)
    assert t["human_touches"] == 3
    assert t["tokens"] == {"input_tokens": 10, "output_tokens": 5}
    assert t["success_rate"] == pytest.approx(1 / 3, abs=1e-3)
    assert t["first_useful_result_s"] is not None
    j, md = run.write(str(tmp_path))
    data = json.loads(open(j).read())
    assert data["totals"]["human_touches"] == 3 and len(data["steps"]) == 4
    text = open(md).read()
    assert "| pair-phone | pair | fail |" in text and "RuntimeError: boom" in text
    assert "touch: type the code" in text


def test_toy_seed_has_exactly_the_advertised_bug(tmp_path):
    for path, text in toy_seed.FILES.items():
        p = tmp_path / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    run = lambda: subprocess.run([sys.executable, "-m", "unittest", "-q"], cwd=tmp_path,
                                 capture_output=True, text=True)
    broken = run()
    assert broken.returncode != 0 and "test_median_even" in broken.stderr
    stats = tmp_path / "toy" / "stats.py"
    stats.write_text(stats.read_text().replace(
        "    return xs[mid]", "    return xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2"))
    assert run().returncode == 0


def test_proxmox_provider_only_touches_its_own_vms(monkeypatch):
    p = ProxmoxProvider({"host": "root@hv", "image": "/img.qcow2", "ssh_key": "/k",
                         "vmid_range": [900, 902]}, log=lambda *_: None)
    confs = {
        900: f"name: {NAME_PREFIX}laptop-900\ntags: {TAG}\n",
        901: "name: someones-vm\ntags: rook-cleanroom\n",                # wrong name
        902: f"name: {NAME_PREFIX}hub-902\n",                                # no tag
        108: f"name: {NAME_PREFIX}old\ntags: {TAG}\n",                       # out of range
    }
    out = "".join(f"== /etc/pve/qemu-server/{k}.conf\n{v}" for k, v in confs.items())

    class R:
        def __init__(self, stdout):
            self.stdout, self.returncode = stdout, 0
    monkeypatch.setattr(p, "_pve", lambda cmd, **kw: R(out))
    assert p._owned() == {900: f"{NAME_PREFIX}laptop-900"}
    for vmid in (901, 902, 108):
        with pytest.raises(ProviderError):
            p._check_owned(vmid)
    p._check_owned(900)
