//! What the app's worker controller (`worker05`) shares: its status for the
//! settings page and the tray, and finding a worker's processes by their
//! command line.

use serde::Serialize;
use std::path::Path;
use std::process::{Command, Stdio};

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct Info {
    /// "core2" (the app runs the worker), or "none" when unknown.
    pub mode: &'static str,
    /// "running", "stopped", "not-installed" or "unknown".
    pub status: &'static str,
    pub detail: String,
    pub can_start: bool,
    pub can_stop: bool,
}

impl Info {
    /// When the status couldn't be read at all.
    pub fn unknown() -> Self {
        Info {
            mode: "none",
            status: "unknown",
            detail: "Couldn't check this computer's worker.".into(),
            can_start: false,
            can_stop: false,
        }
    }
}

/// The rows whose command line names `path` as a whole argument, where
/// `accept` likes what follows it.
pub fn pids_naming(rows: &str, path: &Path, windows: bool, accept: fn(&str) -> bool) -> Vec<u32> {
    let fold = |s: &str| if windows { s.to_lowercase() } else { s.to_string() };
    let want = fold(&path.display().to_string());
    rows.lines()
        .filter_map(|l| {
            // A byte-order mark may lead the first row (Windows PowerShell).
            let (pid, cmd) = l
                .trim_start_matches('\u{feff}')
                .trim_start()
                .split_once(char::is_whitespace)?;
            let cmd = fold(cmd);
            let edge = |c: Option<char>| c.map_or(true, |c| c.is_whitespace() || c == '"' || c == '\'');
            let found = cmd.match_indices(want.as_str()).any(|(at, _)| {
                let (before, after) = (&cmd[..at], &cmd[at + want.len()..]);
                edge(before.chars().next_back()) && edge(after.chars().next()) && accept(after)
            });
            found.then_some(())?;
            pid.trim().parse().ok()
        })
        .collect()
}

/// Processes whose command line names `path` (see [`pids_naming`]).
pub fn processes_naming(path: &Path, accept: fn(&str) -> bool) -> Vec<u32> {
    if cfg!(windows) {
        // UTF-8, so a profile path with non-ASCII letters (C:\\Users\\José) matches.
        const SCRIPT: &str = "[Console]::OutputEncoding = New-Object Text.UTF8Encoding $false; \
            Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | \
            ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }";
        match run(
            "powershell",
            &[
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                SCRIPT,
            ],
        ) {
            Some((true, out)) => pids_naming(&out, path, true, accept),
            _ => vec![],
        }
    } else {
        // -ww: whole command lines (macOS cuts them at the terminal width).
        match run("ps", &["-ww", "-axo", "pid=,args="]) {
            Some((true, out)) => pids_naming(&out, path, false, accept),
            _ => vec![],
        }
    }
}

fn quiet(cmd: &mut Command) -> &mut Command {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW: no console flash
    }
    cmd.stdin(Stdio::null())
}

fn run(program: &str, args: &[&str]) -> Option<(bool, String)> {
    let out = quiet(&mut Command::new(program)).args(args).output().ok()?;
    Some((out.status.success(), String::from_utf8_lossy(&out.stdout).into_owned()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn enrolled_after(after: &str) -> bool {
        after.split_whitespace().any(|a| a.trim_matches('"') == "--enrolled")
    }

    #[test]
    fn processes_by_exact_command_line() {
        let worker_pids = |rows: &str, pyz: &Path, windows: bool| pids_naming(rows, pyz, windows, enrolled_after);
        // A worker an update restarted outside its Scheduled Task (and the
        // child it relaunched), beside other Python processes.
        let pyz = PathBuf::from(r"C:\Users\me\.rook-band-worker\band-worker.pyz");
        let out = "7452\t\"C:\\Program Files\\Python311\\python.exe\" C:\\tools\\other.py\r\n\
            45928\tC:\\Users\\me\\.rook-band-worker\\venv\\Scripts\\pythonw.exe C:\\Users\\me\\.rook-band-worker\\band-worker.pyz --enrolled --ws\r\n\
            116568\tC:\\Users\\me\\.rook-band-worker\\venv\\Scripts\\pythonw.exe \"C:\\Users\\ME\\.rook-band-worker\\Band-Worker.pyz\" --enrolled\r\n\
            501\tpython C:\\dev\\rook\\band-worker.pyz --enrolled\r\n\
            502\tpython C:\\Users\\me\\.rook-band-worker\\band-worker.pyz --hub 127.0.0.1:7474\r\n\
            503\tpython C:\\Users\\me\\.rook-band-worker\\band-worker.pyz.bak --enrolled\r\n\
            504\tpython D:\\x\\C:\\Users\\me\\.rook-band-worker\\band-worker.pyz --enrolled\r\n\
            505\tpython --enrolled C:\\Users\\me\\.rook-band-worker\\band-worker.pyz --ws\r\n\
            9\t\r\n\
            notapid\tpython band-worker.pyz --enrolled\r\n";
        assert_eq!(worker_pids(out, &pyz, true), vec![45928, 116568]);
        assert!(worker_pids("", &pyz, true).is_empty());
        // ps on Linux and macOS: case matters, and only this install's bundle.
        let pyz = PathBuf::from("/home/me/.rook-band-worker/band-worker.pyz");
        let ps = "  812 /home/me/.rook-band-worker/venv/bin/python /home/me/.rook-band-worker/band-worker.pyz --enrolled --ws\n\
                  913 /home/me/rook/.venv/bin/python /home/me/rook/band-worker.pyz --enrolled\n\
                  914 python -m pytest tests/test_band-worker.pyz.py\n\
                  915 /home/me/.rook-band-worker/venv/bin/python /home/ME/.rook-band-worker/band-worker.pyz --enrolled\n\
                  916 python /tmp/x/home/me/.rook-band-worker/band-worker.pyz --enrolled\n\
                  917 python '/home/me/.rook-band-worker/band-worker.pyz' --enrolled\n";
        assert_eq!(worker_pids(ps, &pyz, false), vec![812, 917]);
        // A profile with non-ASCII letters (the PowerShell query reads UTF-8).
        let pyz = PathBuf::from(r"C:\Users\José\.rook-band-worker\band-worker.pyz");
        let out = "77\tC:\\Users\\José\\.rook-band-worker\\venv\\Scripts\\pythonw.exe C:\\Users\\JOSÉ\\.rook-band-worker\\band-worker.pyz --enrolled --ws\r\n";
        assert_eq!(worker_pids(out, &pyz, true), vec![77]);
        let bom = format!("\u{feff}{out}");
        assert_eq!(worker_pids(&bom, &pyz, true), vec![77]);
    }
}
