"""Manifest 1.2 in rook.kit.manifest: provides as a list (B3), record aliases (C5)."""
from __future__ import annotations

from rook.kit.contracts import Contracts
from rook.kit.manifest.load import check_text, normalize

KVM = """\
manifest: 1
plugin: kvm
version: 1.0.0
caps:
  screen.capture: { doc: Shot., risk: read, scope: local, run: scripts/k.py:shot }
  status: { doc: Is it up., risk: read, scope: local, run: scripts/k.py:status }
provides: [screen.capture]
records:
  kvm:
    fields: { name: { type: str } }
    history: true
    aliases: { list: [pikvm.list], history: [pikvm.history] }
"""


def test_list_provides_normalizes_to_full_names_and_marks_the_cap():
    data, issues = check_text(KVM)
    assert not [i for i in issues if i.level == "error"]
    norm = normalize(data)
    assert norm["provides"] == {"screen.capture": "kvm.screen.capture"}
    assert norm["caps"]["kvm.screen.capture"]["contracts"] == ["screen.capture"]
    assert "contracts" not in norm["caps"]["kvm.status"]
    assert normalize(norm)["provides"] == norm["provides"]      # idempotent


def test_map_provides_may_name_a_cap_relative_to_the_plugin():
    data, _ = check_text(KVM.replace("provides: [screen.capture]", "provides: { screen.capture: screen.capture }"))
    assert normalize(data)["provides"] == {"screen.capture": "kvm.screen.capture"}


def test_record_aliases_land_on_the_generated_caps():
    caps = normalize(check_text(KVM)[0])["caps"]
    assert caps["kvm.list"]["aliases"] == ["pikvm.list"]
    assert caps["kvm.history"]["aliases"] == ["pikvm.history"]
    assert caps["kvm.get"]["aliases"] == []


def test_contracts_registry_reads_the_list_form():
    c = Contracts()
    c.load(normalize(check_text(KVM)[0]) | {"caps": {"screen.capture": {}, "status": {}}})
    assert c.providers("screen.capture") == ["kvm.screen.capture"]
    c2 = Contracts()
    c2.load({"plugin": "kvm", "provides": ["screen.capture"], "caps": {"screen.capture": {}}})
    assert c2.providers("screen.capture") == ["kvm.screen.capture"]
