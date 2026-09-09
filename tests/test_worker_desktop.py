"""Desktop install/OTA integration without changing a real home or service."""
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

from rook.worker import desktop


@pytest.fixture
def installed(tmp_path, monkeypatch):
    # Spaces, quotes and shell substitutions must remain literal path characters.
    home = tmp_path / "user's home $(false)"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("ANDROID_ARGUMENT", raising=False)
    bundle = home / ".rook-band-worker/band-worker.pyz"
    bundle.parent.mkdir()
    bundle.touch()
    monkeypatch.setattr(sys, "argv", [str(bundle), "--enrolled"])
    return home, bundle


def write_bundle(path, version):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("__main__.py", f"import json,sys; print(json.dumps([{version},sys.argv[1:]]))")


def test_launcher_uses_updated_bundle_and_quotes_arguments(installed):
    home, bundle = installed
    write_bundle(bundle, 1)
    result = desktop.install_launcher()
    assert result["installed"]
    launcher = home / ".local/bin/rook"
    args = ["band", "--url", "https://example.com/a space?x=$(false)"]
    first = subprocess.check_output([str(launcher), *args], text=True)
    assert json.loads(first) == [1, ["--cli", *args]]
    # Existing launcher follows an atomic OTA replacement, without reinstalling.
    replacement = bundle.with_suffix(".new")
    write_bundle(replacement, 2)
    replacement.replace(bundle)
    assert json.loads(subprocess.check_output([str(launcher), "--version"], text=True)) == [2, ["--cli", "--version"]]
    assert not (home / ".config/rook/band.conf").exists()


def test_path_setup_is_idempotent_and_preserves_shell_config(installed):
    home, _ = installed
    profile = home / ".bashrc"
    profile.write_text("# my existing config\n")
    desktop.install_launcher()
    original = profile.read_text()
    desktop.install_launcher()
    assert profile.read_text() == original
    assert original.startswith("# my existing config\n")
    env = {**os.environ, "PATH": "/usr/bin:/bin"}
    output = subprocess.check_output(["/bin/bash", "-c", '. "$HOME/.bashrc"; command -v rook'], env=env, text=True)
    assert output.strip() == str(home / ".local/bin/rook")
    assert desktop.MARKER in (home / ".config/fish/conf.d/rook-cli.fish").read_text()


def test_preserves_unrelated_command_and_migrates_standalone(installed):
    home, _ = installed
    launcher = home / ".local/bin/rook"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\necho unrelated\n")
    assert not desktop.install_launcher()["installed"]
    assert launcher.read_text().endswith("echo unrelated\n")
    old = '#!/usr/bin/env python3\n"""Full-screen terminal control panel for the worker band."""\n'
    launcher.write_text(old)
    assert desktop.install_launcher()["installed"]
    assert launcher.with_name("rook.pre-worker-cli").read_text() == old


def test_skips_source_and_native_android(installed, monkeypatch):
    home, _ = installed
    monkeypatch.setenv("ANDROID_ARGUMENT", "native-app")
    assert not desktop.install_launcher()["installed"]
    monkeypatch.delenv("ANDROID_ARGUMENT")
    monkeypatch.setattr(sys, "argv", ["/tmp/staged.pyz", "--selftest"])
    assert not desktop.install_launcher()["installed"]
    assert not (home / ".local").exists()


def test_windows_launcher_uses_console_python(installed, monkeypatch):
    home, _ = installed
    monkeypatch.setattr(desktop.sys, "platform", "win32")
    monkeypatch.setattr(desktop.sys, "executable", str(home / "venv/Scripts/pythonw.exe"))
    paths = []
    monkeypatch.setattr(desktop, "_windows_path", paths.append)
    assert desktop.install_launcher()["installed"]
    text = (home / ".local/bin/rook.cmd").read_text()
    assert 'python.exe"' in text and "pythonw.exe" not in text
    assert "--cli %*" in text and paths == [home / ".local/bin"]


def test_dispatch_keeps_dashboard_and_worker_separate(monkeypatch):
    from rook.worker import cli
    from rook.cli import band_tui
    calls = []
    monkeypatch.setattr(band_tui, "main", lambda: calls.append("dashboard"))
    monkeypatch.setattr(sys, "argv", ["bundle.pyz", "--cli"])
    cli.main()
    assert calls == ["dashboard"]
    monkeypatch.setattr(cli, "main", lambda: calls.append(list(sys.argv)))
    monkeypatch.setattr(sys, "argv", ["rook", "worker", "--enrolled"])
    desktop.main()
    assert calls[-1] == ["rook", "--enrolled"]


def test_worker_boot_installs_cli_but_version_does_not(monkeypatch):
    from rook.worker import cli, enroll
    calls = []
    monkeypatch.setattr(desktop, "ensure_launcher", lambda: calls.append("installed"))
    monkeypatch.setattr(sys, "argv", ["bundle.pyz", "--version"])
    cli.main()
    assert calls == []
    monkeypatch.setattr(enroll, "load", lambda: {})
    monkeypatch.setattr(sys, "argv", ["bundle.pyz"])
    with pytest.raises(SystemExit):  # Missing PSK: no network contact.
        cli.main()
    assert calls == ["installed"]
