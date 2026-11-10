//! shell.exec, shell.which, shell.env: the contract of rook/plugins/shell
//! (plugin.yaml, scripts/shell.py), `cmd` run by sh, bash, PowerShell or cmd.

use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use regex::Regex;
use serde_json::{json, Map, Value};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWriteExt};
use tokio::process::{Child, Command};

use super::{Args, Reply};
use crate::streams::{Body, Streams};

const OUT_MAX: usize = 8 << 20;
const DENIED: [&str; 10] = [
    "permission denied",
    "operation not permitted",
    "access is denied",
    "must be run as root",
    "are you root",
    "requires root",
    "eacces",
    "eperm",
    "requires elevation",
    "administrator privileges",
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
    if f.fract() == 0.0 {
        format!("{f:.0}")
    } else {
        format!("{f}")
    }
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

/// The process and its group: TERM, up to 2 s for the process, then KILL
/// what is left of the group (a child that outlives its shell too).
async fn kill(child: &mut Child) {
    #[cfg(unix)]
    if let Some(pid) = child.id() {
        unsafe { libc::killpg(pid as i32, libc::SIGTERM) };
        let _ = tokio::time::timeout(Duration::from_secs(2), child.wait()).await;
        unsafe { libc::killpg(pid as i32, libc::SIGKILL) };
    }
    let _ = child.kill().await;
}

fn decode(b: &[u8]) -> String {
    String::from_utf8_lossy(b).into_owned()
}

/// The program to run and the shell that runs `cmd` (none for argv).
fn command(
    argv: Option<Vec<String>>,
    cmd: Option<String>,
    shell: Option<String>,
) -> Result<(Vec<String>, Option<&'static str>), String> {
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
            vec![
                bin,
                "-NoLogo".into(),
                "-NoProfile".into(),
                "-NonInteractive".into(),
                "-Command".into(),
                script,
            ]
        }
        "cmd" if cfg!(windows) => {
            let comspec = std::env::var("COMSPEC").unwrap_or_else(|_| "cmd.exe".into());
            vec![comspec, "/d".into(), "/s".into(), "/c".into(), cmd.into()]
        }
        "cmd" => return Err("cmd.exe only exists on Windows workers".into()),
        _ if cfg!(windows) => {
            let bin = git_sh(name).ok_or(format!(
                "no {name} on this worker (install Git for Windows; WSL's bash is not used)"
            ))?;
            vec![bin, "-c".into(), cmd.into()]
        }
        "bash" => vec![
            which_path("bash")
                .ok_or("bash is not installed on this worker")?
                .to_string_lossy()
                .into(),
            "-c".into(),
            cmd.into(),
        ],
        _ => vec!["/bin/sh".into(), "-c".into(), cmd.into()],
    };
    Ok((argv, Some(name)))
}

fn powershell() -> Option<String> {
    let names: &[&str] = if cfg!(windows) {
        &["pwsh", "powershell"]
    } else {
        &["pwsh"]
    };
    if let Some(found) = names.iter().find_map(|n| which_path(n)) {
        return Some(found.to_string_lossy().into());
    }
    if cfg!(windows) {
        let root = std::env::var("SystemRoot").unwrap_or_else(|_| r"C:\Windows".into());
        let path: PathBuf = [root.as_str(), "System32", "WindowsPowerShell", "v1.0", "powershell.exe"]
            .iter()
            .collect();
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
        p.metadata()
            .map(|m| m.is_file() && m.permissions().mode() & 0o111 != 0)
            .unwrap_or(false)
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
            Ok(Value::Object(
                vars.into_iter()
                    .map(|(k, v)| (k.clone(), json!(shown(&k, v))))
                    .collect(),
            ))
        }
        _ => Err("give name or prefix".into()),
    }
}

/// shell.open: a command (default: a login shell) as a live stream, on a
/// terminal (pty=true) or on pipes with output merged. Output becomes
/// `bytes` events; input events are typed, signalled or resize it; the
/// stream closes with the exit code.
pub fn open(a: Args<'_>, streams: &Arc<Streams>, participants: Vec<String>, attrs: Map<String, Value>) -> Reply {
    let (mut argv, mut cmd) = (a.strings("argv")?, a.str("cmd")?);
    if argv.is_none() && cmd.is_none() {
        argv = Some(if cfg!(windows) {
            vec![
                powershell().unwrap_or_else(|| "powershell.exe".into()),
                "-NoLogo".into(),
            ]
        } else {
            vec![std::env::var("SHELL").unwrap_or_else(|_| "/bin/sh".into()), "-l".into()]
        });
        cmd = None;
    }
    let (argv, ran) = command(argv, cmd, a.str("shell")?)?;
    let pty = a.bool("pty", true)?;
    if pty && cfg!(windows) {
        return Err("pty is not available on Windows workers yet; use pty=false".into());
    }
    let size = (
        a.int("cols", 120)?.clamp(1, 1000) as u16,
        a.int("rows", 32)?.clamp(1, 1000) as u16,
    );
    let cwd = a.str("cwd")?;
    let title = a
        .str("title")?
        .filter(|t| !t.is_empty())
        .unwrap_or_else(|| argv.join(" "));
    let mut c = std::process::Command::new(&argv[0]);
    c.args(&argv[1..]).envs(a.env("env")?);
    if let Some(dir) = &cwd {
        c.current_dir(dir);
    }
    let (child, output, master) = if pty {
        spawn_pty(&mut c, size)
    } else {
        spawn_pipes(&mut c)
    }
    .map_err(|e| spawn_error(e, &argv[0], cwd.as_deref()))?;
    let here = std::env::current_dir()
        .map(|d| d.to_string_lossy().into_owned())
        .unwrap_or_default();
    let mut meta = attrs;
    meta.insert("cwd".into(), json!(cwd.unwrap_or(here)));
    meta.insert("pty".into(), json!(pty));
    meta.insert("argv".into(), json!(argv));
    if let Some(shell) = ran {
        meta.insert("shell".into(), json!(shell));
    }
    let pid = child.id();
    meta.insert("pid".into(), json!(pid));
    let opened = streams.open(
        if pty { "terminal" } else { "process" },
        &title,
        "shell.open",
        participants,
        meta,
    );
    let sid = opened.id.clone();
    let _ = streams.append(
        &sid,
        "status",
        Body::Json(json!({"state": "started", "pid": pid})),
        "shell.open",
    );
    let child = Arc::new(Mutex::new(child));
    let input = output.input;
    {
        let child = child.clone();
        std::thread::spawn(move || feed(opened.input, input, master, child));
    }
    let streams = streams.clone();
    let mut out = output.read;
    std::thread::spawn(move || {
        let mut buf = vec![0u8; 64 * 1024];
        loop {
            match out.read(&mut buf) {
                Ok(0) | Err(_) => break, // EOF, or EIO once a pty's last writer is gone
                Ok(n) => {
                    if streams
                        .append(&sid, "bytes", Body::Bytes(buf[..n].to_vec()), "shell.open")
                        .is_err()
                    {
                        signal(&child, "KILL"); // closed under it (stream.close)
                        break;
                    }
                }
            }
        }
        let code = loop {
            if let Ok(Some(status)) = child.lock().unwrap().try_wait() {
                break Some(exit_code(&status));
            }
            std::thread::sleep(Duration::from_millis(50));
        };
        let mut status = Map::new();
        status.insert("exit".into(), json!(code));
        streams.close(&sid, "shell.open", status);
    });
    Ok(json!({"stream": opened.id}))
}

struct Pipes {
    read: Box<dyn Read + Send>,
    input: Box<dyn Write + Send>,
}

type Spawned = (std::process::Child, Pipes, Option<Master>);

fn spawn_pipes(c: &mut std::process::Command) -> std::io::Result<Spawned> {
    let (read, write) = std::io::pipe()?;
    c.stdin(Stdio::piped()).stdout(write.try_clone()?).stderr(write);
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        c.process_group(0);
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        c.creation_flags(0x0800_0000);
    }
    let mut child = c.spawn()?;
    let stdin = child.stdin.take().unwrap();
    Ok((
        child,
        Pipes {
            read: Box::new(read),
            input: Box::new(stdin),
        },
        None,
    ))
}

#[cfg(unix)]
type Master = std::os::fd::OwnedFd;
#[cfg(not(unix))]
type Master = ();

#[cfg(unix)]
fn spawn_pty(c: &mut std::process::Command, (cols, rows): (u16, u16)) -> std::io::Result<Spawned> {
    use std::os::fd::{FromRawFd, OwnedFd};
    use std::os::unix::process::CommandExt;
    let (mut m, mut s) = (0, 0);
    let ws = libc::winsize {
        ws_row: rows,
        ws_col: cols,
        ws_xpixel: 0,
        ws_ypixel: 0,
    };
    if unsafe { libc::openpty(&mut m, &mut s, std::ptr::null_mut(), std::ptr::null(), &ws) } != 0 {
        return Err(std::io::Error::last_os_error());
    }
    let (master, slave) = unsafe { (OwnedFd::from_raw_fd(m), OwnedFd::from_raw_fd(s)) };
    c.stdin(Stdio::from(slave.try_clone()?))
        .stdout(Stdio::from(slave.try_clone()?))
        .stderr(Stdio::from(slave));
    // A new session with the pty as its controlling terminal: job control and Ctrl-C work.
    unsafe {
        c.pre_exec(|| {
            if libc::setsid() < 0 || libc::ioctl(0, libc::TIOCSCTTY as _, 0) < 0 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
    let child = c.spawn()?;
    let read = std::fs::File::from(master.try_clone()?);
    let input = std::fs::File::from(master.try_clone()?);
    Ok((
        child,
        Pipes {
            read: Box::new(read),
            input: Box::new(input),
        },
        Some(master),
    ))
}

#[cfg(not(unix))]
fn spawn_pty(_: &mut std::process::Command, _: (u16, u16)) -> std::io::Result<Spawned> {
    Err(std::io::Error::other(
        "pty is not available on Windows workers yet; use pty=false",
    ))
}

/// Input events to the process until the stream closes.
fn feed(
    events: std::sync::mpsc::Receiver<Value>,
    mut input: Box<dyn Write + Send>,
    master: Option<Master>,
    child: Arc<Mutex<std::process::Child>>,
) {
    while let Ok(body) = events.recv() {
        if let Some(data) = crate::streams::input_data(&body) {
            if input.write_all(&data).and_then(|_| input.flush()).is_err() {
                return; // the process closed its input
            }
        } else if let Some(sig) = body["signal"].as_str() {
            signal(&child, sig);
        } else if let (Some(cols), Some(rows)) = (body["resize"][0].as_u64(), body["resize"][1].as_u64()) {
            #[cfg(unix)]
            if let Some(m) = &master {
                use std::os::fd::AsRawFd;
                let ws = libc::winsize {
                    ws_row: rows.min(1000) as u16,
                    ws_col: cols.min(1000) as u16,
                    ws_xpixel: 0,
                    ws_ypixel: 0,
                };
                unsafe { libc::ioctl(m.as_raw_fd(), libc::TIOCSWINSZ, &ws) };
            }
            #[cfg(not(unix))]
            let _ = (&master, cols, rows);
        }
    }
}

/// A signal to the process's group; on Windows every signal ends it.
fn signal(child: &Arc<Mutex<std::process::Child>>, name: &str) {
    #[cfg(unix)]
    {
        let sig = match name {
            "INT" => libc::SIGINT,
            "TERM" => libc::SIGTERM,
            "KILL" => libc::SIGKILL,
            "QUIT" => libc::SIGQUIT,
            _ => libc::SIGHUP,
        };
        let pid = child.lock().unwrap().id();
        unsafe { libc::killpg(pid as i32, sig) };
    }
    #[cfg(not(unix))]
    {
        let _ = name;
        let _ = child.lock().unwrap().kill();
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;

    #[tokio::test]
    async fn a_timeout_kills_the_whole_group_even_after_the_shell_is_gone() {
        let dir = std::env::temp_dir().join(format!("rook-shell-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let cases = [
            "sleep 30 & echo $! > {pid}", // the shell is gone before the timeout
            "(trap '' TERM; exec sleep 30) & echo $! > {pid}; wait", // the shell dies on TERM, its child doesn't
        ];
        for (i, cmd) in cases.iter().enumerate() {
            let pidfile = dir.join(format!("pid{i}"));
            let a = json!({"cmd": cmd.replace("{pid}", pidfile.to_str().unwrap()), "timeout": 0.5});
            let err = exec(Args(a.as_object().unwrap())).await.unwrap_err();
            assert!(err.starts_with("timed out after 0.5s"), "{err}");
            let pid: i32 = std::fs::read_to_string(&pidfile).unwrap().trim().parse().unwrap();
            let mut gone = false;
            for _ in 0..100 {
                if unsafe { libc::kill(pid, 0) } != 0 {
                    gone = true;
                    break;
                }
                tokio::time::sleep(Duration::from_millis(50)).await;
            }
            if !gone {
                unsafe { libc::kill(pid, libc::SIGKILL) };
            }
            assert!(gone, "{cmd}: the child outlived the timeout");
        }
        let _ = std::fs::remove_dir_all(&dir);
    }
}
