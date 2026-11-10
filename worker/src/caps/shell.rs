//! shell.exec, shell.which, shell.env: the contract of rook/plugins/shell
//! (plugin.yaml, scripts/shell.py), `cmd` run by sh, bash, PowerShell or cmd.

use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;

use regex::Regex;
use serde_json::{json, Map, Value};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWriteExt};
use tokio::process::{Child, Command};

use super::{Args, Reply};

const OUT_MAX: usize = 8 << 20;
const DENIED: [&str; 10] = [
    "permission denied", "operation not permitted", "access is denied", "must be run as root", "are you root",
    "requires root", "eacces", "eperm", "requires elevation", "administrator privileges",
];
const PS_PRELUDE: &str = "$ProgressPreference='SilentlyContinue'\n\
    try{$__u=New-Object Text.UTF8Encoding $false;[Console]::OutputEncoding=$__u;\
    [Console]::InputEncoding=$__u;$OutputEncoding=$__u}catch{}\n";
const PS_EPILOGUE: &str = "\nif($?){exit 0}elseif($LASTEXITCODE){exit $LASTEXITCODE}else{exit 1}";

pub async fn exec(a: Args<'_>) -> Reply {
    let (argv, ran) = command(a.strings("argv")?, a.str("cmd")?, a.str("shell")?)?;
    let stdin = a.str("stdin")?;
    let timeout = a.float("timeout", 30.0)?;
    let cwd = a.str("cwd")?;
    let mut cmd = Command::new(&argv[0]);
    cmd.args(&argv[1..])
        .envs(a.env("env")?)
        .stdin(if stdin.is_some() { Stdio::piped() } else { Stdio::null() })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    if let Some(dir) = &cwd {
        cmd.current_dir(dir);
    }
    #[cfg(unix)]
    cmd.process_group(0);
    #[cfg(windows)]
    cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    let mut child = cmd.spawn().map_err(|e| spawn_error(e, &argv[0], cwd.as_deref()))?;
    let run = collect(&mut child, stdin);
    let (out, err, cut) = match tokio::time::timeout(Duration::from_secs_f64(timeout.max(0.0)), run).await {
        Ok(got) => got.map_err(|e| e.to_string())?,
        Err(_) => {
            kill(&mut child).await;
            return Err(format!(
                "timed out after {}s and was killed; anything it already did still happened. Use shell.open for long jobs.",
                g(timeout)
            ));
        }
    };
    let status = child.wait().await.map_err(|e| e.to_string())?;
    let code = exit_code(&status);
    let stderr = decode(&err);
    let mut reply = Map::new();
    reply.insert("ok".into(), json!(code == 0));
    reply.insert("code".into(), json!(code));
    reply.insert("stdout".into(), json!(decode(&out)));
    if cut {
        reply.insert("truncated".into(), json!(true));
    }
    if let Some(shell) = ran.filter(|s| *s != "sh" || cfg!(windows)) {
        reply.insert("shell".into(), json!(shell));
    }
    let low = stderr.to_lowercase();
    if code == 126 || (code != 0 && DENIED.iter().any(|m| low.contains(m))) {
        reply.insert("denied".into(), json!(true));
    }
    reply.insert("stderr".into(), json!(stderr));
    Ok(Value::Object(reply))
}

/// Python's `{:g}` for the timeout in the message.
fn g(f: f64) -> String {
    if f.fract() == 0.0 { format!("{f:.0}") } else { format!("{f}") }
}

fn exit_code(status: &std::process::ExitStatus) -> i64 {
    #[cfg(unix)]
    {
        use std::os::unix::process::ExitStatusExt;
        if let Some(sig) = status.signal() {
            return -(sig as i64);
        }
    }
    status.code().unwrap_or(-1) as i64
}

fn spawn_error(e: std::io::Error, program: &str, cwd: Option<&str>) -> String {
    match (e.kind(), cwd) {
        (std::io::ErrorKind::NotFound, Some(dir)) if !Path::new(dir).is_dir() => format!("not found: {dir}"),
        (std::io::ErrorKind::NotFound, _) => format!("not found: {program}"),
        (std::io::ErrorKind::NotADirectory, Some(dir)) => format!("not a directory: {dir}"),
        _ => e.to_string(),
    }
}

/// stdout and stderr, each its first 8 MiB; the rest is read and dropped so
/// a chatty process can't fill memory.
async fn collect(child: &mut Child, stdin: Option<String>) -> std::io::Result<(Vec<u8>, Vec<u8>, bool)> {
    let mut input = child.stdin.take();
    let feed = async move {
        if let (Some(pipe), Some(text)) = (input.as_mut(), stdin) {
            let _ = pipe.write_all(text.as_bytes()).await;
        }
        drop(input);
    };
    let (out, err) = (child.stdout.take().unwrap(), child.stderr.take().unwrap());
    let ((), (out, cut1), (err, cut2)) = tokio::join!(feed, drain(out), drain(err));
    Ok((out?, err?, cut1 || cut2))
}

async fn drain(mut r: impl AsyncRead + Unpin) -> (std::io::Result<Vec<u8>>, bool) {
    let (mut buf, mut cut, mut chunk) = (Vec::new(), false, vec![0u8; 64 * 1024]);
    loop {
        match r.read(&mut chunk).await {
            Ok(0) => return (Ok(buf), cut),
            Ok(n) => {
                let room = OUT_MAX.saturating_sub(buf.len());
                cut |= n > room;
                buf.extend_from_slice(&chunk[..n.min(room)]);
            }
            Err(e) => return (Err(e), cut),
        }
    }
}

/// The process and its group: TERM, 2 s, then KILL.
async fn kill(child: &mut Child) {
    #[cfg(unix)]
    if let Some(pid) = child.id() {
        unsafe { libc::killpg(pid as i32, libc::SIGTERM) };
        if tokio::time::timeout(Duration::from_secs(2), child.wait()).await.is_ok() {
            return;
        }
        unsafe { libc::killpg(pid as i32, libc::SIGKILL) };
    }
    let _ = child.kill().await;
}

fn decode(b: &[u8]) -> String {
    String::from_utf8_lossy(b).into_owned()
}

/// The program to run and the shell that runs `cmd` (none for argv).
fn command(argv: Option<Vec<String>>, cmd: Option<String>, shell: Option<String>) -> Result<(Vec<String>, Option<&'static str>), String> {
    match (argv, cmd) {
        (Some(_), Some(_)) => Err("give argv or cmd, not both".into()),
        (None, None) => Err("give argv or cmd".into()),
        (Some(argv), None) => {
            if shell.is_some() {
                return Err("shell applies to cmd; argv runs with no shell".into());
            }
            if argv.is_empty() {
                return Err("argv is empty".into());
            }
            let mut argv = argv;
            if cfg!(windows) {
                if let Some(found) = which_path(&argv[0]) {
                    argv[0] = found.to_string_lossy().into_owned();
                }
            }
            Ok((argv, None))
        }
        (None, Some(cmd)) => shell_argv(&cmd, shell.as_deref()),
    }
}

fn shell_argv(cmd: &str, shell: Option<&str>) -> Result<(Vec<String>, Option<&'static str>), String> {
    let asked = shell.unwrap_or("").trim().to_lowercase();
    let name: &'static str = match asked.as_str() {
        "" if cfg!(windows) => "powershell",
        "" | "sh" | "/bin/sh" | "sh.exe" => "sh",
        "bash" | "bash.exe" => "bash",
        "powershell" | "pwsh" | "powershell.exe" | "pwsh.exe" => "powershell",
        "cmd" | "cmd.exe" => "cmd",
        _ => return Err("shell must be one of sh, bash, powershell, cmd".into()),
    };
    let argv = match name {
        "powershell" => {
            let bin = powershell().ok_or("PowerShell is not installed on this worker")?;
            let script = format!("{PS_PRELUDE}{cmd}{PS_EPILOGUE}");
            vec![bin, "-NoLogo".into(), "-NoProfile".into(), "-NonInteractive".into(), "-Command".into(), script]
        }
        "cmd" if cfg!(windows) => {
            let comspec = std::env::var("COMSPEC").unwrap_or_else(|_| "cmd.exe".into());
            vec![comspec, "/d".into(), "/s".into(), "/c".into(), cmd.into()]
        }
        "cmd" => return Err("cmd.exe only exists on Windows workers".into()),
        _ if cfg!(windows) => {
            let bin = git_sh(name).ok_or(format!("no {name} on this worker (install Git for Windows; WSL's bash is not used)"))?;
            vec![bin, "-c".into(), cmd.into()]
        }
        "bash" => vec![which_path("bash").ok_or("bash is not installed on this worker")?.to_string_lossy().into(), "-c".into(), cmd.into()],
        _ => vec!["/bin/sh".into(), "-c".into(), cmd.into()],
    };
    Ok((argv, Some(name)))
}

fn powershell() -> Option<String> {
    let names: &[&str] = if cfg!(windows) { &["pwsh", "powershell"] } else { &["pwsh"] };
    if let Some(found) = names.iter().find_map(|n| which_path(n)) {
        return Some(found.to_string_lossy().into());
    }
    if cfg!(windows) {
        let root = std::env::var("SystemRoot").unwrap_or_else(|_| r"C:\Windows".into());
        let path: PathBuf = [root.as_str(), "System32", "WindowsPowerShell", "v1.0", "powershell.exe"].iter().collect();
        if path.is_file() {
            return Some(path.to_string_lossy().into());
        }
    }
    None
}

/// Git for Windows' sh or bash; a bare bash on PATH is usually WSL's launcher.
fn git_sh(name: &str) -> Option<String> {
    let mut roots: Vec<PathBuf> = vec![];
    if let Some(git) = which_path("git") {
        roots.extend(git.ancestors().skip(2).take(2).map(Path::to_path_buf));
    }
    for var in ["ProgramFiles", "ProgramW6432", "LOCALAPPDATA"] {
        if let Ok(v) = std::env::var(var) {
            roots.push(Path::new(&v).join("Git"));
            roots.push(Path::new(&v).join("Programs").join("Git"));
        }
    }
    for root in roots {
        for sub in [Path::new("bin").to_path_buf(), Path::new("usr").join("bin")] {
            let path = root.join(sub).join(format!("{name}.exe"));
            if path.is_file() {
                return Some(path.to_string_lossy().into());
            }
        }
    }
    let found = which_path(name)?.to_string_lossy().into_owned();
    let low = found.to_lowercase();
    (!low.contains("\\system32\\") && !low.contains("\\windowsapps\\")).then_some(found)
}

/// A program on PATH, as `shutil.which` finds it (on Windows also name.exe, name.cmd).
pub fn which_path(name: &str) -> Option<PathBuf> {
    let candidates: Vec<String> = if cfg!(windows) && Path::new(name).extension().is_none() {
        vec![name.into(), format!("{name}.exe"), format!("{name}.cmd")]
    } else {
        vec![name.into()]
    };
    let path = std::env::var_os("PATH")?;
    for candidate in &candidates {
        if Path::new(candidate).components().count() > 1 {
            return runnable(Path::new(candidate)).then(|| PathBuf::from(candidate));
        }
        for dir in std::env::split_paths(&path) {
            let p = dir.join(candidate);
            if runnable(&p) {
                return Some(p);
            }
        }
    }
    None
}

fn runnable(p: &Path) -> bool {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        p.metadata().map(|m| m.is_file() && m.permissions().mode() & 0o111 != 0).unwrap_or(false)
    }
    #[cfg(not(unix))]
    p.is_file()
}

pub fn which(a: Args<'_>) -> Reply {
    Ok(which_path(&a.need("name")?).map_or(Value::Null, |p| json!(p.to_string_lossy())))
}

pub fn env(a: Args<'_>) -> Reply {
    let secret = Regex::new(r"(?i)(TOKEN|SECRET|PASSW|PSK|API_?KEY|PRIVATE|CREDENTIAL|_KEY$|^ROOK_)").unwrap();
    let shown = |k: &str, v: String| if secret.is_match(k) { "***".to_string() } else { v };
    match (a.str("name")?, a.str("prefix")?) {
        (Some(name), None) => Ok(match std::env::var(&name) {
            Ok(v) => json!(shown(&name, v)),
            Err(_) => a.str("default")?.map_or(Value::Null, Value::String),
        }),
        (None, Some(prefix)) => {
            let mut vars: Vec<(String, String)> = std::env::vars().filter(|(k, _)| k.starts_with(&prefix)).collect();
            vars.sort();
            Ok(Value::Object(vars.into_iter().map(|(k, v)| (k.clone(), json!(shown(&k, v)))).collect()))
        }
        _ => Err("give name or prefix".into()),
    }
}
