"""Core guardrails after the change-management reset (rook/hub/hardstops.py,
knowledge change-management-design-20261025): with advanced security off
(the default) only the hard stops and sacred trust entries ask; agents never
change the guardrails; merges go on the gates and the author's self-review.
The old behaviour, with the plugin on, is what test_approvals, test_trust,
test_relay and test_table_votes keep checking."""
from __future__ import annotations

import pytest

from rook.hub import approvals as A
from rook.hub import hardstops as H
from rook.hub import trust as T
from rook.hub.authz import Authorizer, principal_for_user
from rook.hub.plugins import security
from rook.hub.policy import PolicyStore, Principal
from tests.test_trust import BAKE_KAIJU, check, seeded

AGENT = Principal("token:claude", "token", "agent", label="claude code")
OPERATOR = Principal("token:ops", "token", "operator", label="ops")
OWNER = principal_for_user({"id": "u1", "username": "bake"}, True)


def core(tmp_path, *, store=None):
    ap = A.Approvals(str(tmp_path / "approvals.db"), A.Config(wait_secs=0),
                     trust=store if store is not None else seeded())
    return Authorizer(PolicyStore(str(tmp_path / "policy.json")), None, approvals=ap)


def on(az, cap, target="cachyrig", args=None, principal=AGENT):
    if target == "rook":
        return az.check(cap, "rook", {"name": "rook", "roles": ["is_hub"]}, principal=principal,
                        local=True, args=args)
    return az.check(cap, target, {"worker_id": target, "name": target, "caps": [cap]},
                    principal=principal, args=args)


# -- what destroys data for good --------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    "rm -rf /", "sudo rm -rf /home/bake", "rm -r ~", "rm -fr $HOME/*", "rm -rf /var/lib/rook-band-mcp",
    "mkfs.ext4 /dev/sdb1", "wipefs -a /dev/nvme0n1", "dd if=/dev/zero of=/dev/sda bs=1M",
    "sqlite3 x.db 'DROP TABLE calls'", "psql -c 'drop database rook'", "zfs destroy tank/data",
    "docker volume rm pgdata", "find /home/bake -name '*.db' -delete", "shred -u secrets.txt",
    "cd /tmp && rm -rf /etc", "rm /var/lib/rook-band-mcp/approvals.db", "rm -f music.sqlite3"])
def test_commands_that_destroy_data_for_good_are_seen(cmd):
    assert H.destroys(cmd), cmd


@pytest.mark.parametrize("cmd", [
    "ls -la /", "rm -rf build", "rm -rf /tmp/rook-test-123", "rm notes.txt",
    "rm -rf ~/rook-worktrees/cm-a", "git status", "sqlite3 x.db 'select * from calls'",
    "systemctl --user restart rook-band-worker", "docker ps", "find . -name '*.pyc' -delete",
    "rm -f /var/lib/rook-band-mcp/old.log", "rm -f /tmp/pytest-1/approvals.db"])
def test_routine_commands_are_not_hard_stops(cmd):
    assert not H.destroys(cmd), cmd


def test_the_hard_stops_by_cap_and_args():
    assert "erases data" in H.stop("memory.forget", {})
    assert "erases data" in H.stop("job.write", {"action": "delete"})
    assert not H.stop("job.write", {"action": "run"})
    assert "reveals a secret" in H.stop("secret.get", {"name": "x"})
    assert "sends a message as you" in H.stop("sms.send", {"to": "+1555", "text": "hi"})
    assert "sends a message as you" in H.stop("notify.act", {"key": "k", "text": "on my way"})
    assert not H.stop("notify.act", {"key": "k", "action": "mark as read"})
    assert "reveal a secret" in H.stop("notify.post", {"text": "pw {{secret:github-pat}}"})
    assert not H.stop("notify.post", {"title": "done", "text": "deploy finished"})
    assert "deletes" in H.stop("shell.exec", {"argv": ["sh", "-c", "rm -rf /srv/media"]})
    assert not H.stop("shell.exec", {"argv": ["uptime"]})
    assert "erases data" not in H.stop("console.write", {"data": "ls\n"})
    assert H.stop("console.write", {"data": "dropdb rook\n"})


def test_reaching_a_machine_over_ssh_and_the_hubs_own_files():
    assert H.remote_hosts("ssh -p 22 -o BatchMode=yes bake@kaiju 'sudo systemctl restart x'") \
        == {"kaiju"}
    assert H.remote_hosts("scp f.tar kaiju.local:/tmp/ && rsync -a d/ ubuntu@hub:/srv/") == \
        {"kaiju.local", "hub"}
    assert not H.remote_hosts("git clone https://github.com/x/y")
    assert "hub's own secrets" in H.stop("shell.exec", {"cmd": "sudo cat /var/lib/rook-band-mcp/vault.key"})
    assert "hub's own secrets" in H.stop("file.write", {"path": "/var/lib/rook-band-mcp/trust.json"})
    assert not H.stop("file.read", {"path": "/home/bake/notes.md"})


@pytest.mark.parametrize("cmd", [
    'bash -lc "ssh kaiju sudo reboot"', "sh -c 'ssh kaiju sudo reboot'", "echo `ssh kaiju reboot`",
    "x=$(ssh kaiju reboot)", "/usr/bin/ssh kaiju sudo reboot", "ssh -tp 2222 kaiju reboot",
    "ssh -o BatchMode=yes -i ~/.ssh/k bake@kaiju.local reboot", 'ssh cachyrig "ssh kaiju reboot"',
    "ssh -t hop ssh kaiju reboot", "ssh -J kaiju other uptime", "\\ssh kaiju reboot",
    "ssh -vvvvp 2222 kaiju reboot", "ssh -o ProxyJump=kaiju other uptime",
    "ssh -oProxyJump=bake@kaiju:22 other uptime", "ssh -Jkaiju other uptime"])
def test_quoted_wrapped_and_path_prefixed_ssh_still_reaches_kaiju(cmd):
    assert {"kaiju", "kaiju.local"} & H.remote_hosts(cmd), cmd


@pytest.mark.parametrize("cmd", [
    'ssh cachyrig "rm -rf /home/bake"', "bash -c 'rm -rf ~'", "/bin/rm -rf /home/bake",
    "echo `rm -rf /srv/media`", "sudo -n rm -rf /var/lib/rook-band-mcp", "\\rm -rf /home/bake"])
def test_quoted_and_path_prefixed_rm_is_seen(cmd):
    assert H.destroys(cmd), cmd


def test_sending_as_the_person_by_any_messaging_family():
    for cap in ("mail.send", "gmail.reply", "slack.post", "whatsapp.forward", "call.dial"):
        assert "sends a message as you" in H.stop(cap, {"to": "x"}), cap
    for cap in ("mail.list", "gmail.read", "slack.search", "msg.send", "notify.post",
                "voice.speak", "chat.send"):
        assert not H.stop(cap, {"text": "hi"}), cap


def test_deeply_nested_ssh_stops_without_crashing():
    assert H.remote_hosts("ssh a " * 3000 + "ssh kaiju reboot") == {"a"}


def test_every_command_cap_is_checked():
    for cap in ("shell.exec", "proc.start", "work.stream.open"):
        assert "deletes" in H.stop(cap, {"cmd": "rm -rf /home/bake"}), cap
        assert H.remote_hosts(H.command_text(cap, {"argv": ["ssh", "kaiju", "reboot"]})) == {"kaiju"}
    assert H.stop("proc.write", {"data": "dropdb music\n"})


def test_guardrails_are_named():
    assert H.guardrail("policy.set", {})
    assert H.guardrail("settings.set", {"key": "core.approvals.enabled", "value": True})
    assert H.guardrail("settings.set", {"key": "security.enabled", "value": True})
    assert H.guardrail("hub.plugin.enable", {"module": "security"})
    assert not H.guardrail("hub.plugin.enable", {"module": "jobs"})
    assert not H.guardrail("settings.set", {"key": "home.voice_model", "value": "x"})


# -- the authorizer in core -------------------------------------------------------------

def test_core_asks_only_for_hard_stops_and_sacred_machines(tmp_path):
    az = core(tmp_path)
    d = on(az, "shell.exec", "kaiju", {"argv": ["systemctl", "restart", "strata"]})
    assert d.held and d.hold_kind == "trust" and BAKE_KAIJU in d.hold_reason
    assert not on(az, "file.read", "kaiju").held             # reads on kaiju go
    assert not on(az, "shell.exec", "bakenetcanada", {"argv": ["uptime"]}).held   # guarded: go
    assert not on(az, "worker.update", "cachyrig").held      # admin: go
    assert not on(az, "hub.plugin.enable", "rook", {"module": "jobs"}).held
    d = on(az, "shell.exec", "cachyrig", {"argv": ["rm", "-rf", "/home/bake"]})
    assert d.held and d.hold_kind == "default" and d.hold_reason.startswith("hard stop:")
    assert on(az, "secret.get", "rook", {"name": "github-pat"}).held
    assert on(az, "sms.send", "bakephone", {"to": "+15550100", "text": "hi"}).held
    assert not on(az, "notify.post", "bakephone", {"text": "done"}).held
    d = on(az, "shell.exec", "cachyrig", {"cmd": "ssh kaiju sudo systemctl restart strata"})
    assert d.held and d.hold_kind == "trust" and "over ssh" in d.hold_reason
    d = on(az, "proc.start", "cachyrig", {"cmd": 'bash -lc "ssh kaiju sudo reboot"'})
    assert d.held and d.hold_kind == "trust"                 # the console's way in too
    assert not on(az, "shell.exec", "cachyrig", {"cmd": "ssh pianobar uptime"}).held
    for p in (OWNER,):                                       # people are never held
        assert not on(az, "shell.exec", "kaiju", {"argv": ["reboot"]}, principal=p).held


def test_agents_never_change_the_guardrails(tmp_path):
    az = core(tmp_path)
    for cap, args in (("policy.set", {"doc": {}}),
                      ("settings.set", {"key": "core.approvals.enabled", "value": True}),
                      ("settings.reset", {"key": "core.approvals.hold"}),
                      ("hub.plugin.enable", {"module": "security"}),
                      ("hub.plugin.disable", {"module": "security"}),
                      ("trust.tighten", {"op": "set_unlisted"})):
        d = on(az, cap, "rook", args, principal=OPERATOR)
        assert d.denied and d.rule == "hardstop:guardrail", cap
    assert not on(az, "policy.set", "rook", {"doc": {}}, principal=OWNER).denied
    assert not on(az, "settings.set", "rook", {"key": "home.voice_model", "value": "x"},
                  principal=OPERATOR).denied


@pytest.mark.parametrize("advanced", [False, True])
def test_operator_hold_rules_are_ignored(tmp_path, advanced):
    (tmp_path / "policy.json").write_text(
        '{"version": 1, "rules": [{"id": "h", "who": "*", "hold": "shell.*", "on": "*"}]}')
    ap = A.Approvals(str(tmp_path / "approvals.db"), A.Config(wait_secs=0, advanced=advanced),
                     trust=seeded())
    az = Authorizer(PolicyStore(str(tmp_path / "policy.json")), None, approvals=ap)
    d = on(az, "shell.exec", "cachyrig", {"argv": ["uptime"]})
    assert not d.held and "hold rules are retired" in d.reason


def test_the_plugin_adds_guarded_trust_asks_and_no_blanket_holds(tmp_path):
    ap = A.Approvals(str(tmp_path / "approvals.db"),
                     A.Config(wait_secs=0, advanced=True), trust=seeded())
    az = Authorizer(PolicyStore(str(tmp_path / "policy.json")), None, approvals=ap)
    assert check(az, "shell.exec", "bakenetcanada").hold_kind == "trust"      # guarded asks
    assert not check(az, "worker.update", "cachyrig").held                     # admin goes
    assert not on(az, "proc.close", "cachyrig", {"pid": 1}).held                # destructive goes
    for i in range(12):                                                        # no spread limit
        assert not on(az, "shell.exec", f"w{i}", {"argv": ["uptime"]}).held
    d = on(az, "secret.get", "rook", {"name": "x"})
    assert d.held                                                              # the stops stay


@pytest.mark.asyncio
async def test_a_plugin_tools_inner_guardrail_call_is_refused(tmp_path):
    from types import SimpleNamespace

    from rook.hub.authz import current_principal
    from rook.hub.mcp_tools import _hold_gate
    az = core(tmp_path)
    node = SimpleNamespace(client=SimpleNamespace(authz=az), worker_id="rook",
                           entry=lambda: {"name": "rook", "roles": ["is_hub"]})
    tok = current_principal.set(OPERATOR)
    try:
        with pytest.raises(PermissionError, match="guardrail"):
            await _hold_gate(node, "settings.set", {"key": "core.approvals.enabled", "value": 1})
        await _hold_gate(node, "settings.set", {"key": "home.voice_model", "value": "x"})
    finally:
        current_principal.reset(tok)


def test_the_switch_reads_env_then_the_plugin_switch_then_its_setting(tmp_path):
    from rook.hub.settings_store import SettingsStore
    st = SettingsStore(tmp_path / "settings.db")
    assert not security.enabled(st, {})
    st.set("security.enabled", "hub", "", value=True, actor="human:u1")
    assert security.enabled(st, {})
    st.set("core.plugins.hub", "hub", "", value={"security": False}, actor="human:u1")
    assert not security.enabled(st, {})
    assert security.enabled(st, {"ROOK_SECURITY_ADVANCED": "1"})
    assert security.enabled(st, {"ROOK_SECURITY_ENABLED": "true"})
    assert A.load_config(st, {"ROOK_SECURITY_ADVANCED": "1"}).advanced
    assert not A.load_config(st, {}).advanced


# -- trust: one proposal for a new model, and no ratchet ---------------------------------

def test_a_batch_proposal_applies_whole_when_the_person_accepts():
    st = seeded()
    change = {"op": "batch", "data": {"changes": [
        {"op": "delete_entry", "data": {"id": "rook-hub"}},
        {"op": "delete_entry", "data": {"id": "pianobar-super"}}]}}
    said = T.describe_change(st.current(), change)
    assert said["loosens"] and "rook-hub" in said["sentence"]
    prop = st.propose(change, by="claude", why="Bake: only the hard stops remain")
    ids = {e["id"] for e in st.current()["entries"]}
    assert {"rook-hub", "pianobar-super"} <= ids                # nothing changes yet
    st.change("decide_proposal", {"id": prop["id"], "accept": True},
              T.Actor("person", "Bake", words="yes, the new model"))
    ids = {e["id"] for e in st.current()["entries"]}
    assert "rook-hub" not in ids and "pianobar-super" not in ids and "kaiju" in ids
    with pytest.raises(T.TrustError, match="only the person"):
        st.change("batch", change["data"], T.Actor("house", "rook", words="Bake said so"))
