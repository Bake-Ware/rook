//! file.read, write, list, search, stat: the contract of rook/plugins/file
//! (plugin.yaml, scripts/file.py). Encodings: utf-8, latin-1, ascii, base64.
//! search uses the regex crate (no look-around or backreferences).

use std::fs::{self, File, Metadata, OpenOptions};
use std::io::{self, BufRead, BufReader, Read, Write};
use std::path::Path;
use std::time::{Duration, Instant, UNIX_EPOCH};

use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine;
use serde_json::{json, Map, Value};

use super::{Args, Reply};

const SEARCH_SECONDS: u64 = 50;

#[derive(Clone, Copy)]
enum Enc {
    Utf8,
    Latin1,
    Ascii,
    Base64,
}

fn encoding(name: &str) -> Result<Enc, String> {
    match name.to_lowercase().replace('_', "-").as_str() {
        "utf-8" | "utf8" => Ok(Enc::Utf8),
        "latin-1" | "latin1" | "iso-8859-1" | "iso8859-1" => Ok(Enc::Latin1),
        "ascii" | "us-ascii" => Ok(Enc::Ascii),
        "base64" => Ok(Enc::Base64),
        _ => Err(format!("unknown encoding: {name}")),
    }
}

fn error(e: io::Error, path: &str) -> String {
    let what = match e.kind() {
        io::ErrorKind::NotFound => "not found",
        io::ErrorKind::IsADirectory => "is a directory",
        io::ErrorKind::NotADirectory => "not a directory",
        io::ErrorKind::PermissionDenied => "permission denied",
        io::ErrorKind::AlreadyExists => "exists",
        _ => return format!("{path}: {e}"),
    };
    format!("{what}: {path}")
}

pub fn read(a: Args<'_>) -> Reply {
    let path = a.need("path")?;
    let name = a.str("encoding")?.unwrap_or_else(|| "utf-8".into());
    let max = a.int("max_bytes", 8 << 20)? as usize;
    let enc = encoding(&name)?;
    let mut opts = OpenOptions::new();
    opts.read(true);
    #[cfg(unix)]
    {
        // Non-blocking open: a FIFO would wait for a writer forever.
        use std::os::unix::fs::OpenOptionsExt;
        opts.custom_flags(libc::O_NONBLOCK);
    }
    let f = opts.open(&path).map_err(|e| error(e, &path))?;
    let meta = f.metadata().map_err(|e| error(e, &path))?;
    if !meta.is_file() {
        return Err(format!("not a regular file: {path}"));
    }
    let mut raw = Vec::new();
    f.take(max as u64 + 1)
        .read_to_end(&mut raw)
        .map_err(|e| error(e, &path))?;
    let mut out = Map::new();
    out.insert("size".into(), json!(meta.len()));
    out.insert("encoding".into(), json!(name));
    if raw.len() > max {
        raw.truncate(max);
        out.insert("truncated".into(), json!(true));
    }
    let content = match enc {
        Enc::Base64 => B64.encode(&raw),
        Enc::Utf8 => String::from_utf8_lossy(&raw).into_owned(),
        Enc::Latin1 => raw.iter().map(|&b| b as char).collect(),
        Enc::Ascii => raw
            .iter()
            .map(|&b| if b < 128 { b as char } else { '\u{fffd}' })
            .collect(),
    };
    out.insert("content".into(), json!(content));
    Ok(Value::Object(out))
}

pub fn write(a: Args<'_>) -> Reply {
    let path = a.need("path")?;
    let content = a.need("content")?;
    let name = a.str("encoding")?.unwrap_or_else(|| "utf-8".into());
    let raw = match encoding(&name)? {
        Enc::Base64 => B64.decode(&content).map_err(|_| "content is not base64".to_string())?,
        Enc::Utf8 => content.into_bytes(),
        Enc::Latin1 => narrow(&content, 256, &name)?,
        Enc::Ascii => narrow(&content, 128, &name)?,
    };
    if a.bool("create_parents", false)? {
        if let Some(parent) = Path::new(&path).parent().filter(|p| !p.as_os_str().is_empty()) {
            fs::create_dir_all(parent).map_err(|e| error(e, &path))?;
        }
    }
    let append = a.bool("append", false)?;
    let mut f = OpenOptions::new()
        .write(true)
        .create(true)
        .append(append)
        .truncate(!append)
        .open(&path)
        .map_err(|e| error(e, &path))?;
    f.write_all(&raw).map_err(|e| error(e, &path))?;
    Ok(json!({"path": path, "written": raw.len()}))
}

fn narrow(text: &str, limit: u32, name: &str) -> Result<Vec<u8>, String> {
    text.chars()
        .map(|c| u8::try_from(c as u32).ok().filter(|_| (c as u32) < limit))
        .collect::<Option<Vec<u8>>>()
        .ok_or(format!(
            "content can't be written as {name}: ordinal not in range({limit})"
        ))
}

fn meta(m: &Metadata) -> Map<String, Value> {
    let ft = m.file_type();
    let kind = if ft.is_dir() {
        "dir"
    } else if ft.is_symlink() {
        "symlink"
    } else if ft.is_file() {
        "file"
    } else {
        "other"
    };
    let modified = m
        .modified()
        .ok()
        .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
        .map_or(0.0, |d| d.as_secs_f64());
    let mut out = Map::new();
    out.insert("type".into(), json!(kind));
    out.insert("size".into(), json!(m.len()));
    out.insert("modified".into(), json!(modified));
    out.insert("mode".into(), json!(format!("0o{:o}", mode(m))));
    out
}

#[cfg(unix)]
fn mode(m: &Metadata) -> u32 {
    use std::os::unix::fs::PermissionsExt;
    m.permissions().mode() & 0o7777
}

/// What Python's os.stat reports on Windows: read-only or not.
#[cfg(not(unix))]
fn mode(m: &Metadata) -> u32 {
    if m.is_dir() {
        0o777
    } else if m.permissions().readonly() {
        0o444
    } else {
        0o666
    }
}

pub fn stat(a: Args<'_>) -> Reply {
    let path = a.need("path")?;
    match fs::symlink_metadata(&path) {
        Ok(m) => {
            let mut out = meta(&m);
            out.insert("exists".into(), json!(true));
            Ok(Value::Object(out))
        }
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(json!({"exists": false})),
        Err(e) => Err(error(e, &path)),
    }
}

pub fn list(a: Args<'_>) -> Reply {
    let path = a.need("path")?;
    let recursive = a.bool("recursive", false)?;
    let hidden = a.bool("hidden", true)?;
    let limit = a.int("limit", 5000)? as usize;
    let root = Path::new(&path);
    if !root.is_dir() {
        return Err(format!(
            "{}: {path}",
            if root.exists() { "not a directory" } else { "not found" }
        ));
    }
    let mut entries = vec![];
    let mut todo = vec![root.to_path_buf()];
    while let Some(dir) = todo.pop() {
        for child in fs::read_dir(&dir).map_err(|e| error(e, &path))?.flatten() {
            let name = child.file_name().to_string_lossy().into_owned();
            if !hidden && name.starts_with('.') {
                continue;
            }
            let Ok(m) = child.path().symlink_metadata() else {
                continue;
            };
            if entries.len() >= limit {
                return Ok(json!({"entries": entries, "truncated": true}));
            }
            let mut e = meta(&m);
            e.insert("name".into(), json!(name));
            if recursive {
                let rel = child.path().strip_prefix(root).unwrap().to_string_lossy().into_owned();
                e.insert("path".into(), json!(rel));
                if m.is_dir() {
                    todo.push(child.path());
                }
            }
            entries.push(Value::Object(e));
        }
    }
    Ok(json!({"entries": entries}))
}

pub fn search(a: Args<'_>) -> Reply {
    let pattern = a.need("pattern")?;
    let path = a.str("path")?.unwrap_or_else(|| ".".into());
    let recursive = a.bool("recursive", true)?;
    let glob = a.str("glob")?.unwrap_or_else(|| "*".into());
    let limit = a.int("limit", 100)? as usize;
    let rx = regex::RegexBuilder::new(&pattern)
        .case_insensitive(a.bool("ignore_case", false)?)
        .build()
        .map_err(|e| format!("bad pattern: {e}"))?;
    let glob = glob::Pattern::new(&glob).map_err(|e| format!("bad glob: {e}"))?;
    let root = Path::new(&path);
    if !root.exists() {
        return Err(format!("not found: {path}"));
    }
    let deadline = Instant::now() + Duration::from_secs(SEARCH_SECONDS);
    let mut matches = vec![];
    let mut files = vec![];
    if root.is_file() {
        files.push(root.to_path_buf());
    } else {
        let mut todo = vec![root.to_path_buf()];
        while let Some(dir) = todo.pop() {
            for child in fs::read_dir(&dir).into_iter().flatten().flatten() {
                let Ok(ft) = child.file_type() else { continue };
                if ft.is_dir() && recursive {
                    todo.push(child.path());
                } else if child.path().is_file() && glob.matches(&child.file_name().to_string_lossy()) {
                    files.push(child.path());
                }
            }
        }
    }
    for fp in files {
        if Instant::now() > deadline {
            return Ok(json!({"matches": matches, "truncated": true, "timed_out": true}));
        }
        let Ok(mut f) = File::open(&fp) else { continue };
        let mut head = vec![0u8; 4096];
        let n = f.read(&mut head).unwrap_or(0);
        if head[..n].contains(&0) {
            continue; // binary
        }
        let Ok(f) = File::open(&fp) else { continue };
        for (i, line) in BufReader::new(f).split(b'\n').enumerate() {
            let Ok(line) = line else { break };
            let text = String::from_utf8_lossy(&line);
            let text = text.trim_end_matches('\r');
            if rx.is_match(text) {
                if matches.len() >= limit {
                    return Ok(json!({"matches": matches, "truncated": true}));
                }
                let short: String = text.chars().take(1000).collect();
                matches.push(json!({"path": fp.to_string_lossy(), "line": i + 1, "text": short}));
            }
        }
    }
    Ok(json!({"matches": matches}))
}
