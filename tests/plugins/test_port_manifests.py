"""Every 0.5 plugin port validates with rook.kit.manifest against core's
contracts (no errors, no warnings) and binds in the worker host."""

import pytest

from port_helpers import CONTRACTS, PLUGINS, go
from rook.core2.worker.host import Host
from rook.kit.manifest import check, load, trust_class

# The device ports; other packages' plugins (agents, ...) have their own tests.
PORTS = sorted(p.parent.name for p in PLUGINS.glob("*/plugin.yaml") if "\npackage: devices\n" in p.read_text())


def test_the_ports_are_there():
    assert PORTS == ["adb", "battery", "camera", "cec", "dongle", "file", "hid-desktop", "kvm",
                     "notification-desktop", "screen-desktop", "shell", "torrent", "tv"]


@pytest.mark.parametrize("name", PORTS)
def test_manifest_is_clean(name):
    data, issues = check(PLUGINS / name, CONTRACTS)
    assert data is not None
    assert [str(i) for i in issues] == []
    m = load(PLUGINS / name, CONTRACTS)
    assert m.plugin == name
    assert (PLUGINS / name / "docs" / "manual.md").is_file()


@pytest.mark.parametrize("name", PORTS)
def test_every_cap_says_risk_scope_and_doc(name):
    m = load(PLUGINS / name, CONTRACTS)
    for cap, spec in m.caps.items():
        assert spec["doc"] and spec["risk"] and spec["scope"] == "local", cap
        assert ("action" in spec) != ("run" in spec), cap
        if spec["risk"] == "read":
            assert "physical" not in spec.get("effects", ()), cap
        # Can lose unsaved work. A TV switched off loses nothing (tv.power.off).
        if cap.endswith(("delete", "power.reset", "api_post")) or cap == "power.off" and name == "kvm":
            assert "destructive" in spec["effects"], cap


CONFIG_ONLY = {"battery", "cec", "notification-desktop", "screen-desktop", "torrent"}


def test_only_config_plugins_are_data():
    # Config-only plugins are data; a plugin with scripts is code (review, signing).
    classes = {name: trust_class(PLUGINS / name) for name in PORTS}
    assert classes == {name: "config" if name in CONFIG_ONLY else "code" for name in PORTS}


def test_scripts_start_processes_only_through_launch():
    # Spec 7.1: plugin code starts processes through core's launch path.
    import re
    banned = re.compile(r"create_subprocess_|subprocess\.(run|Popen|call|check_)|os\.(system|popen|spawn|exec)")
    for script in PLUGINS.glob("*/scripts/*.py"):
        assert not banned.search(script.read_text()), script


@pytest.mark.parametrize("name", PORTS)
def test_binds_in_the_worker_host(name):
    async def body():
        host = Host(os_name="linux", hardware={"display", "battery"})
        p = await host.load(PLUGINS / name)
        assert p.name == name
        # Every cap is either served or unavailable with a reason (a missing bin here).
        served = {c.name for c in p.caps.values()}
        assert served | set(p.unavailable) == set(p.manifest["caps"])
        assert all(p.unavailable.values())
        await host.close()
    go(body)
