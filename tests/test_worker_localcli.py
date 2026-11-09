"""The `rook` command on a worker machine (rook.worker.localcli) and the
running worker's local control link (rook.worker.control)."""

import json
import os
import shutil
import subprocess
import sys
import time

import pytest

from rook.worker import admin, control, localcli


def run(argv, capsys):
    code = localcli.main(argv)
    out = capsys.readouterr()
    return code, out.out, out.err


# -- status / doctor -----------------------------------------------------------------


def _probe(skew=0.0, error=None):
    return lambda url: (None if error else time.time() - skew, error, 0.05)


# -- the hub: ask, reads -------------------------------------------------------------


@pytest.fixture
def dns(monkeypatch):
    """Names resolve only from this table (anything else: no such host)."""
    import socket
    table = {"hub.tail1234.ts.net": ["100.101.102.103"], "hub.corp.test": ["10.1.2.3", "fd00::5"],
             "mixed.example.com": ["10.1.2.3", "8.8.8.8"]}

    def getaddrinfo(host, *a, **k):
        if host not in table:
            raise socket.gaierror("no such host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 0)) for ip in table[host]]
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return table


class _Tty:
    def __init__(self, answer):
        self.answer = answer

    def isatty(self):
        return True

    def readline(self):
        return self.answer + "\n"


# -- plugins ---------------------------------------------------------------------------


# -- logs, pair, completion, dispatch ------------------------------------------------


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish", "powershell"])
def test_completion_scripts(shell, capsys):
    code, out, _ = run(["completion", shell], capsys)
    assert code == 0 and "status" in out and "doctor" in out
    out.encode("ascii")
    if shell == "bash" and shutil.which("bash"):
        subprocess.run(["bash", "-n"], input=out, text=True, check=True)
        # Completing `rook pl` and `rook plugins dis` in a real bash.
        probe = out + ('COMP_WORDS=(rook pl); COMP_CWORD=1; _rook; echo "${COMPREPLY[@]}";'
                       'COMP_WORDS=(rook plugins dis); COMP_CWORD=2; _rook; echo "${COMPREPLY[@]}"')
        res = subprocess.run(["bash", "-c", probe], capture_output=True, text=True, check=True)
        assert res.stdout.split("\n")[:2] == ["plugins", "disable"]
    if shell == "powershell":
        assert "Register-ArgumentCompleter" in out


def test_powershell_completion_completes(tmp_path, capsys):
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("pwsh is not installed")
    run(["completion", "powershell"], capsys)
    script = tmp_path / "comp.ps1"
    script.write_text(localcli.completion_script("powershell"))
    probe = (f". '{script}'; "
             "foreach ($s in @('rook pl', 'rook plugins dis', 'rook plugins disable hi')) {"
             " (TabExpansion2 -inputScript $s -cursorColumn $s.Length).CompletionMatches |"
             " % CompletionText }")
    res = subprocess.run([pwsh, "-NoProfile", "-Command", probe], capture_output=True,
                         text=True, timeout=120)
    assert res.stdout.split() == ["plugins", "disable", "hid"], (res.stdout, res.stderr)
