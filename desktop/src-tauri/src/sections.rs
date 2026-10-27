//! The dashboard's five sections (Feed, Work, Intelligence sources, Trust,
//! Settings) and their pages, for the tray's Dashboard menu. The list is
//! `ui/sections.json`, which the Rook panel also reads;
//! tests/test_desktop_contract.py keeps it in step with the hub's sidebar.

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Page {
    pub hash: String,
    pub title: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Section {
    pub id: String,
    pub title: String,
    pub pages: Vec<Page>,
}

#[derive(Deserialize)]
struct File {
    sections: Vec<Section>,
}

const JSON: &str = include_str!("../../ui/sections.json");

pub fn all() -> Vec<Section> {
    serde_json::from_str::<File>(JSON)
        .expect("ui/sections.json is valid (checked by tests)")
        .sections
}

/// The tray menu item id for a page (`page:<hash>`), and back.
pub fn menu_id(hash: &str) -> String {
    format!("page:{hash}")
}

/// The dashboard path a tray item opens, if it names one of the pages (so a
/// menu id can never become an arbitrary path).
pub fn path_for(menu_id: &str) -> Option<String> {
    let hash = menu_id.strip_prefix("page:")?;
    all()
        .iter()
        .flat_map(|s| &s.pages)
        .any(|p| p.hash == hash)
        .then(|| format!("/#{hash}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn five_sections_with_safe_hashes() {
        let s = all();
        let ids: Vec<&str> = s.iter().map(|s| s.id.as_str()).collect();
        assert_eq!(ids, ["feed", "work", "intelligence", "trust", "settings"]);
        for p in s.iter().flat_map(|s| &s.pages) {
            assert!(
                !p.hash.is_empty() && p.hash.chars().all(|c| c.is_ascii_lowercase() || c == '-'),
                "{}",
                p.hash
            );
            assert!(!p.title.is_empty());
        }
    }

    #[test]
    fn menu_ids_map_to_known_pages_only() {
        assert_eq!(path_for(&menu_id("sessions")).as_deref(), Some("/#sessions"));
        assert_eq!(path_for("page:nope"), None);
        assert_eq!(path_for("page:../etc"), None);
        assert_eq!(path_for("sessions"), None);
    }
}
