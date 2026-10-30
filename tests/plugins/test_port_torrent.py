"""torrent (Deluge) through the worker host, with a fake deluge-console on
PATH. The fake answers like deluge-console 2.2 (outputs copied from a real
one and from deluge/ui/console/cmdline/commands/status.py)."""

import pytest

from port_helpers import ME, PLUGINS, go
from rook.core2.worker.host import CallError, Host

H1, H2 = "a" * 40, "0123456789abcdef0123456789abcdef01234567"

FAKE = f'''
cmd = argv[0] if argv else ""
if os.environ.get("FAKE_DOWN"):
    print("Could not connect to daemon: 127.0.0.1:58846")
    print(" Password does not match")
elif cmd == "--version":
    print("deluge-console 2.2.0\\nlibtorrent: 2.0.13.0\\nPython: 3.14.5\\nOS: Linux")
elif cmd == "status":
    print("Total upload: 1.5 KiB/s\\nTotal download: 0.0 KiB/s\\nDHT Nodes: 254\\nTotal torrents: 2")
    for state, n in [("Allocating", 0), ("Checking", 0), ("Downloading", 1), ("Seeding", 1),
                     ("Paused", 0), ("Error", 0), ("Queued", 0), ("Moving", 0)]:
        print(f" {{state}}: {{n}}")
elif cmd == "info":
    print("[S]  100.00% ubuntu-24.04.iso {H1}")
    print("    DL: 4.5 GiB (0 B) UL: 71.6 MiB (0 B) ETA: -")
    print("[D]   42.5% Some Show S01E01 {H2}")
elif cmd.startswith("info -v "):
    print("Name: ubuntu-24.04.iso\\nDownload Folder: /srv/torrents")
else:
    print("ok: " + cmd)
'''


@pytest.fixture
def deluge(bins):
    bins.add("deluge-console", FAKE)
    return bins


def run(cap, args=None, *, settings=None):
    async def body():
        host = Host(os_name="linux")
        await host.load(PLUGINS / "torrent")
        if settings:
            await host.set_settings("torrent", local=settings)
        try:
            return await host.call(cap, args or {}, ME)
        finally:
            await host.close()
    return go(body)


def sent(bins):
    """The command text each run handed deluge-console (its one argument)."""
    return [c["argv"] for c in bins.calls("deluge-console")]


def test_version_needs_no_daemon(deluge, monkeypatch):
    monkeypatch.setenv("FAKE_DOWN", "")
    assert run("torrent.version") == {"deluge": "2.2.0", "libtorrent": "2.0.13.0"}


def test_status_is_typed(deluge):
    assert run("torrent.status") == {
        "upload": "1.5 KiB/s", "download": "0.0 KiB/s", "dht_nodes": 254, "torrents": 2,
        "checking": 0, "downloading": 1, "seeding": 1, "paused": 0, "error": 0, "queued": 0}
    assert deluge.calls("deluge-console")[0]["env"]["LANG"] == "C"


def test_list_parses_each_torrent(deluge):
    assert run("torrent.list") == [
        {"state": "S", "progress": 100.0, "name": "ubuntu-24.04.iso", "id": H1},
        {"state": "D", "progress": 42.5, "name": "Some Show S01E01", "id": H2}]
    assert run("deluge.list") == run("torrent.list")   # the 0.4 name still works


def test_daemon_down_is_an_error_not_an_empty_list(deluge, monkeypatch):
    monkeypatch.setenv("FAKE_DOWN", "1")
    for cap, args in [("torrent.list", {}), ("torrent.status", {}), ("torrent.pause", {}),
                      ("torrent.get", {"torrent_id": H1})]:
        with pytest.raises(CallError, match="Could not connect to daemon"):
            run(cap, args)


def test_commands_sent(deluge):
    assert run("torrent.get", {"torrent_id": H1}).startswith("Name: ubuntu-24.04.iso")
    run("torrent.add", {"torrent": "magnet:?xt=urn:btih:" + H1 + "&dn=x"})
    run("torrent.pause")
    run("torrent.resume", {"torrent_id": f"{H1} {H2}"})
    run("torrent.remove", {"torrent_id": "ubuntu-24.04.iso"})
    run("torrent.delete", {"torrent_id": H2})
    assert sent(deluge) == [
        [f"info -v {H1}"], [f"add magnet:?xt=urn:btih:{H1}&dn=x"], ["pause *"],
        [f"resume {H1} {H2}"], ["rm ubuntu-24.04.iso"], [f"rm --remove_data {H2}"]]


@pytest.mark.parametrize("cap,arg", [
    ("torrent.remove", "x; rm --remove_data *"),   # a second console command
    ("torrent.remove", "--remove_data"),           # an option
    ("torrent.remove", "*"),                       # remove everything
    ("torrent.delete", "*"),
    ("torrent.delete", "ubuntu*"),                 # a wildcard could take many
    ("torrent.remove", "a*b"),
    ("torrent.delete", f"{H1};{H2}"),
    ("torrent.pause", "a'b"),                      # unbalanced shlex quoting
    ("torrent.pause", "name\nrm x"),
    ("torrent.get", "back\\slash"),
    ("torrent.get", "-v"),
])
def test_identifiers_cant_smuggle_commands(deluge, cap, arg):
    with pytest.raises(CallError, match="^bad args: torrent_id: .* does not match"):
        run(cap, {"torrent_id": arg})
    assert sent(deluge) == []


@pytest.mark.parametrize("value", ["magnet:?x; rm *", "-x", "has space.torrent", "C:\\x.torrent"])
def test_add_refuses_what_deluge_would_reparse(deluge, value):
    with pytest.raises(CallError, match="^bad args: torrent: "):
        run("torrent.add", {"torrent": value})
    assert sent(deluge) == []


def test_console_setting(bins, tmp_path):
    other = bins.add("my-deluge", FAKE)
    bins.add("deluge-console", "print('wrong one')")
    assert run("torrent.version", settings={"console": str(other)})["deluge"] == "2.2.0"
    assert bins.calls("deluge-console") == []


def test_unavailable_without_deluge_console(bins):
    bins.only_these()

    async def body():
        host = Host(os_name="linux")
        p = await host.load(PLUGINS / "torrent")
        assert p.unavailable["torrent.list"] == "missing bin: deluge-console"
        with pytest.raises(CallError, match="^unavailable: torrent.list: missing bin"):
            await host.call("torrent.list", {}, ME)
        await host.close()
    go(body)
