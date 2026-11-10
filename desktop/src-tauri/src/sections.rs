//! The hub's pages by section, for the tray's Dashboard menu and the Rook
//! panel. The hub lists them itself (`hub05::page_sections`).

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

/// The tray menu item id for a page (`page:<hash>`), and back.
pub fn menu_id(hash: &str) -> String {
    format!("page:{hash}")
}

/// The dashboard path a tray item opens, if it names one of the pages shown
/// (so a menu id can never become an arbitrary path).
pub fn path_for(menu_id: &str, sections: &[Section]) -> Option<String> {
    let hash = menu_id.strip_prefix("page:")?;
    sections
        .iter()
        .flat_map(|s| &s.pages)
        .any(|p| p.hash == hash)
        .then(|| format!("/#{hash}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn menu_ids_map_to_known_pages_only() {
        let hub05 = vec![Section {
            id: "hub".into(),
            title: "Hub".into(),
            pages: vec![Page {
                hash: "/hub/workers".into(),
                title: "Workers".into(),
            }],
        }];
        assert_eq!(path_for("page:/hub/workers", &hub05).as_deref(), Some("/#/hub/workers"));
        assert_eq!(path_for(&menu_id("sessions"), &hub05), None);
        assert_eq!(path_for("page:../etc", &hub05), None);
        assert_eq!(path_for("/hub/workers", &hub05), None);
    }
}
