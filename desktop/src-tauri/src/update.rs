//! Is there a newer desktop app? The hub's `/updates/desktop.json` lists the
//! latest Rook release's `rook-desktop-*` files for this platform. The app's
//! version is its own (not Rook's), so the version is read from the file
//! names (`rook-desktop-0.2.0-x64-setup.exe`), not from the hub's
//! `update_available`, which compares against the release's version. Only
//! links into Rook's GitHub releases are offered ([`RELEASES`]).

use serde::Serialize;

/// Where Rook's releases are published. Only installers from here are
/// offered: a hub (or anyone between the app and a plain-http hub) could
/// otherwise point the app at an installer of its own, and the installers
/// aren't signed yet.
pub const RELEASES: &str = "https://github.com/Bake-Ware/rook/releases/download/";

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct Update {
    pub version: String,
    pub url: String,
    pub name: String,
}

/// The platform name the hub expects.
pub fn platform() -> &'static str {
    if cfg!(windows) {
        "windows"
    } else if cfg!(target_os = "macos") {
        "macos"
    } else {
        "linux"
    }
}

/// The desktop version in a published file name, e.g. `0.2.0` from
/// `rook-desktop-0.2.0-x64-setup.exe` (also the last segment of a URL).
pub fn version_of(file: &str) -> Option<String> {
    let name = file.rsplit('/').next()?;
    let rest = name.strip_prefix("rook-desktop-")?;
    let v: String = rest.chars().take_while(|c| c.is_ascii_digit() || *c == '.').collect();
    let v = v.trim_end_matches('.');
    (v.split('.').count() >= 2 && v.split('.').all(|p| !p.is_empty())).then(|| v.to_string())
}

fn parts(v: &str) -> Vec<u64> {
    v.split('.').map(|p| p.parse().unwrap_or(0)).collect()
}

/// Whether version `a` is newer than `b` (numeric, dotted).
pub fn newer(a: &str, b: &str) -> bool {
    let (mut x, mut y) = (parts(a), parts(b));
    let n = x.len().max(y.len());
    x.resize(n, 0);
    y.resize(n, 0);
    x > y
}

/// How this copy was installed decides which file to offer: the setup
/// program on Windows, the disk image on macOS, the AppImage when running as
/// one (it sets `APPIMAGE`), else the .deb.
fn preference(platform: &str, appimage: bool) -> &'static [&'static str] {
    match platform {
        "windows" => &["-setup.exe", ".msi"],
        "macos" => &[".dmg"],
        _ if appimage => &[".AppImage", ".deb"],
        _ => &[".deb", ".AppImage"],
    }
}

/// The newest download that is newer than `current`, in the form this copy
/// was installed as.
pub fn pick(urls: &[String], current: &str, platform: &str, appimage: bool) -> Option<Update> {
    let mut best: Option<(Update, usize)> = None;
    for url in urls {
        // One release file, <tag>/<name> in plain characters: no query,
        // fragment, encoding or dot-segment tricks.
        let Some(rest) = url.strip_prefix(RELEASES) else {
            continue;
        };
        let safe = |part: &str| {
            !part.is_empty()
                && part
                    .chars()
                    .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-'))
                && !part.starts_with('.')
        };
        if rest.split('/').count() != 2 || !rest.split('/').all(safe) {
            continue;
        }
        let Some(version) = version_of(url) else { continue };
        if !newer(&version, current) {
            continue;
        }
        let name = url.rsplit('/').next().unwrap_or(url).to_string();
        let Some(rank) = preference(platform, appimage).iter().position(|e| name.ends_with(e)) else {
            continue;
        };
        let better = match &best {
            None => true,
            Some((b, r)) => newer(&version, &b.version) || (version == b.version && rank < *r),
        };
        if better {
            best = Some((
                Update {
                    version,
                    url: url.clone(),
                    name,
                },
                rank,
            ));
        }
    }
    best.map(|(u, _)| u)
}

#[cfg(test)]
mod tests {
    use super::*;

    const REL: &str = "https://github.com/Bake-Ware/rook/releases/download/v0.4.0/";

    #[test]
    fn only_rook_release_files_are_offered() {
        for url in [
            "https://evil.example/rook-desktop-9.0.0-x64-setup.exe",
            "http://github.com/Bake-Ware/rook/releases/download/v9/rook-desktop-9.0.0-x64-setup.exe",
            "https://github.com/someone/rook/releases/download/v9/rook-desktop-9.0.0-x64-setup.exe",
            "https://github.com/Bake-Ware/rook/releases/download/v9/x/rook-desktop-9.0.0-x64-setup.exe",
            "https://github.com/Bake-Ware/rook/releases/download/../../evil/rook-desktop-9.0.0-x64-setup.exe",
            "https://github.com/Bake-Ware/rook/releases/download/v9/rook-desktop-9.0.0-x64-setup.exe?x=1",
            "https://github.com.evil.example/Bake-Ware/rook/releases/download/v9/rook-desktop-9.0.0-x64-setup.exe",
            "file:///rook-desktop-9.0.0-x64-setup.exe",
            "https://github.com/Bake-Ware/rook/releases/download/%2e%2e/rook-desktop-9.0.0-x64-setup.exe",
            "https://github.com/Bake-Ware/rook/releases/download/v9/rook-desktop-9.0.0-x64%20setup.exe",
            "https://github.com/Bake-Ware/rook/releases/download/v9/rook-desktop-9.0.0\t-x64-setup.exe",
            "https://github.com/Bake-Ware/rook/releases/download/.v9/rook-desktop-9.0.0-x64-setup.exe",
            "https://github.com/bake-ware/rook/releases/download/v9/rook-desktop-9.0.0-x64-setup.exe",
        ] {
            assert_eq!(pick(&[url.to_string()], "0.1.0", "windows", false), None, "{url}");
        }
        let ok = format!("{RELEASES}v9.0.0/rook-desktop-9.0.0-x64-setup.exe");
        assert_eq!(pick(&[ok], "0.1.0", "windows", false).unwrap().version, "9.0.0");
    }

    fn urls(names: &[&str]) -> Vec<String> {
        names.iter().map(|n| format!("{REL}{n}")).collect()
    }

    #[test]
    fn versions_come_from_file_names() {
        assert_eq!(version_of("rook-desktop-0.2.0-x64-setup.exe").as_deref(), Some("0.2.0"));
        assert_eq!(
            version_of(&format!("{REL}rook-desktop-0.10.1-amd64.AppImage")).as_deref(),
            Some("0.10.1")
        );
        assert_eq!(version_of("Rook_0.2.0_x64-setup.exe"), None);
        assert_eq!(version_of("rook-desktop-x64-setup.exe"), None);
        assert!(newer("0.10.0", "0.9.9"));
        assert!(newer("0.2.1", "0.2"));
        assert!(!newer("0.2.0", "0.2.0"));
        assert!(!newer("0.1.9", "0.2.0"));
    }

    #[test]
    fn picks_the_newest_file_for_this_install() {
        let u = urls(&[
            "rook-desktop-0.2.0-amd64.AppImage",
            "rook-desktop-0.2.0-amd64.deb",
            "rook-desktop-0.2.0-x64-setup.exe",
            "rook-desktop-0.2.0-aarch64.dmg",
        ]);
        assert_eq!(
            pick(&u, "0.1.0", "windows", false).unwrap().name,
            "rook-desktop-0.2.0-x64-setup.exe"
        );
        assert_eq!(
            pick(&u, "0.1.0", "linux", false).unwrap().name,
            "rook-desktop-0.2.0-amd64.deb"
        );
        assert_eq!(
            pick(&u, "0.1.0", "linux", true).unwrap().name,
            "rook-desktop-0.2.0-amd64.AppImage"
        );
        assert_eq!(pick(&u, "0.1.0", "macos", false).unwrap().version, "0.2.0");
        // Up to date, or only older files: nothing to offer.
        assert_eq!(pick(&u, "0.2.0", "windows", false), None);
        assert_eq!(pick(&u, "0.3.0", "linux", false), None);
    }
}
