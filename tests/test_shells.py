"""The shell model behind ``cmd`` (rook/worker/shells.py) and shell.exec.

Windows is simulated by flipping ``shells._IS_WIN``; what only a Windows box
proves (PowerShell's real output and exit codes) is checked on a Windows
worker over the band, see the task linked from the PR.
"""
import os
import subprocess

import pytest

from rook.core.pluginkit import ArgError
from rook.worker import shells
from rook.worker.plugins.shell import ShellPlugin
from rook.worker.plugins import proc as proc_mod

posix = pytest.mark.skipif(os.name != "posix", reason="runs /bin/sh")


@pytest.fixture
def win(monkeypatch, tmp_path):
    """A fake Windows PATH: PowerShell 5.1, Git for Windows, WSL's bash, the
    Store python stub and a real Python."""
    monkeypatch.setattr(shells, "_IS_WIN", True)
    files = {}

    def make(*parts):
        path = tmp_path.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
        return str(path)
    files["powershell"] = make("System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    files["git"] = make("Git", "cmd", "git.exe")
    files["git_sh"] = make("Git", "bin", "sh.exe")
    files["git_bash"] = make("Git", "bin", "bash.exe")
    files["wsl_bash"] = make("System32", "bash.exe")
    files["stub"] = make("Microsoft", "WindowsApps", "python3.exe")
    files["python"] = make("Python311", "python.exe")
    path_dirs = [str(tmp_path / "Microsoft" / "WindowsApps"), str(tmp_path / "Python311")]
    monkeypatch.setenv("PATH", os.pathsep.join(path_dirs))
    found = {"powershell": files["powershell"], "git": files["git"], "bash": files["wsl_bash"],
             "python3": files["stub"], "python": files["python"]}
    monkeypatch.setattr(shells.shutil, "which", lambda name: found.get(name))
    files["found"] = found
    return files


def test_shell_names():
    assert shells.shell_name("") == shells.default_shell()
    assert shells.shell_name("PWSH") == "powershell"
    assert shells.shell_name("cmd.exe") == "cmd"
    with pytest.raises(ValueError, match="sh, bash, powershell, cmd"):
        shells.shell_name("fish")


@posix
def test_posix_shells_are_unchanged():
    assert shells.shell_argv("echo hi") == (["/bin/sh", "-c", "echo hi"], "sh")
    with pytest.raises(ValueError, match="only exists on Windows"):
        shells.shell_argv("dir", "cmd")
    assert shells.resolve_argv(["python3", "-V"]) == ["python3", "-V"]


def test_windows_cmd_runs_in_powershell_with_sh_semantics(win):
    argv, name = shells.shell_argv("ls; python3 -V")
    assert name == "powershell" and argv[:-1] == [win["powershell"], "-NoLogo", "-NoProfile",
                                                   "-NonInteractive", "-Command"]
    script = argv[-1]
    # UTF-8 out of 5.1, no progress records, then the caller's command, then
    # the last command's exit code, read before anything else runs.
    assert script.startswith("$ProgressPreference='SilentlyContinue'\n")
    assert "[Console]::OutputEncoding=$__u" in script and "$OutputEncoding=$__u" in script
    assert f"Set-Alias python3 '{win['python']}'\n" in script     # past the Store stub
    assert "Set-Alias python " not in script                      # already a real one
    assert script.endswith("ls; python3 -V\nif($?){exit 0}elseif($LASTEXITCODE)"
                           "{exit $LASTEXITCODE}else{exit 1}")
    # Never -EncodedCommand: 5.1 then writes its error stream as CLIXML.
    assert "-EncodedCommand" not in argv
    # The command survives MSVCRT quoting intact (what CreateProcess hands PowerShell).
    tricky = 'Write-Output "a \\"b\\" c" \'d\' `$x %PATH% & | < >'
    line = shells.command_line(shells.shell_argv(tricky)[0])
    assert subprocess.list2cmdline([shells.powershell_script(tricky)]) in line


def test_windows_powershell_7_first(win):
    win["found"]["pwsh"] = "C:\\ps7\\pwsh.exe"
    assert shells.shell_argv("ls")[0][0] == "C:\\ps7\\pwsh.exe"


def test_windows_other_shells(win, monkeypatch):
    monkeypatch.setenv("COMSPEC", "C:\\Windows\\system32\\cmd.exe")
    argv, name = shells.shell_argv('echo "hi" & ver', "cmd")
    assert name == "cmd" and argv == ["C:\\Windows\\system32\\cmd.exe", "/d", "/s", "/c", 'echo "hi" & ver']
    # cmd.exe gets the line verbatim inside the one pair of quotes /s strips.
    assert shells.command_line(argv) == '"C:\\Windows\\system32\\cmd.exe" /d /s /c "echo "hi" & ver"'
    # sh and bash are Git for Windows' own; WSL's bash.exe is another machine.
    assert shells.shell_argv("uname", "sh") == ([win["git_sh"], "-c", "uname"], "sh")
    assert shells.shell_argv("uname", "bash")[0][0] == win["git_bash"]
    os.unlink(win["git_bash"])
    os.unlink(win["git_sh"])
    win["found"].pop("git")
    monkeypatch.delenv("ProgramFiles", raising=False)
    monkeypatch.delenv("ProgramW6432", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    with pytest.raises(ValueError, match="Git for Windows"):
        shells.shell_argv("uname", "bash")


def test_windows_too_long_for_a_command_line(win):
    with pytest.raises(ValueError, match=r"\.ps1"):
        shells.shell_argv("x" * 33000)


def test_windows_argv_resolution(win, tmp_path):
    # python3 that only finds the Store stub runs a real interpreter.
    assert shells.resolve_argv(["python3", "-c", "print(1)"]) == [win["python"], "-c", "print(1)"]
    # A real one on PATH is just found (full path, as CreateProcess wants).
    assert shells.resolve_argv(["python", "-V"]) == [win["python"], "-V"]
    # An npm shim runs its target, so cmd.exe never parses the arguments.
    npm = tmp_path / "npm"
    script = npm / "node_modules" / "npm" / "bin" / "npm-cli.js"
    script.parent.mkdir(parents=True)
    script.write_text("//")
    (npm / "node.exe").write_text("")
    shim = npm / "npm.cmd"
    shim.write_text('@ECHO off\r\n"%_prog%"  "%dp0%\\node_modules\\npm\\bin\\npm-cli.js" %*\r\n')
    win["found"]["npm"] = str(shim)
    assert shells.resolve_argv(["npm", "ls"]) == [str(npm / "node.exe"), str(script), "ls"]
    # Unknown names are left for the spawn to report.
    assert shells.resolve_argv(["nope", "x"]) == ["nope", "x"]


def test_decode_falls_back_without_raising():
    assert shells.decode("café ✓".encode()) == "café ✓"
    assert shells.decode(b"caf\x82") == "caf\ufffd"


def test_popen_flags_hide_console_windows(monkeypatch):
    monkeypatch.setattr(shells, "_IS_WIN", False)
    assert shells.popen_flags() == {}
    monkeypatch.setattr(shells, "_IS_WIN", True)
    assert shells.popen_flags() == {"creationflags": shells.CREATE_NO_WINDOW}


# -- shell.exec ------------------------------------------------------------------------

@posix
@pytest.mark.asyncio
async def test_shell_exec_cmd_and_shell_choice():
    p = ShellPlugin()
    r = await p._exec(cmd="echo $((6*7)); exit 3")
    assert r == {"ok": False, "code": 3, "stdout": "42\n", "stderr": ""}    # no "shell" for sh
    r = await p._exec(cmd="echo ${BASH_VERSION:+bash}", shell="bash")
    assert r["stdout"] == "bash\n" and r["shell"] == "bash"
    with pytest.raises(ArgError, match="only exists on Windows"):
        await p._exec(cmd="dir", shell="cmd")
    with pytest.raises(ArgError, match="argv runs with no shell"):
        await p._exec(argv=["true"], shell="bash")
    with pytest.raises(ArgError, match="one of"):
        await p._exec(cmd="true", shell="fish")


@posix
@pytest.mark.asyncio
async def test_proc_start_takes_a_shell_too():
    p = proc_mod.ProcPlugin()
    try:
        r = await p._start(cmd="echo ${BASH_VERSION:+bash}", shell="bash")
        assert r["ok"] and r["shell"] == "bash"
        bad = await p._start(cmd="dir", shell="cmd")
        assert not bad["ok"] and "only exists on Windows" in bad["error"]
        plain = await p._start(cmd="echo plain")
        assert plain["ok"] and "shell" not in plain
    finally:
        await p.stop()
