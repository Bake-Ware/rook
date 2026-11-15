//! What deserves a desktop notification, decided from successive polls.
//!
//! Pure: the poller in lib.rs fetches, this decides. The first poll only
//! records where things stand (no burst of old messages at start-up); later
//! polls report what is new.

use crate::hub::AttentionItem;
use std::collections::HashSet;

#[derive(Debug, Clone, PartialEq)]
pub struct Notice {
    pub title: String,
    pub body: String,
}

/// Most separate notifications from one poll; more become one summary.
const MAX_SEPARATE: usize = 3;

#[derive(Default)]
pub struct Watcher {
    attention: Option<HashSet<String>>,
}

fn clip(text: &str, max: usize) -> String {
    let t = text.trim();
    if t.chars().count() <= max {
        t.to_string()
    } else {
        let mut s: String = t.chars().take(max.saturating_sub(1)).collect();
        s.push('\u{2026}');
        s
    }
}

impl Watcher {
    pub fn new() -> Self {
        Self::default()
    }

    /// New items from the hub's attention list. `None` (the hub has no such
    /// list) changes nothing.
    pub fn attention(&mut self, items: Option<&[AttentionItem]>) -> Vec<Notice> {
        let Some(items) = items else { return vec![] };
        let ids: HashSet<String> = items.iter().map(|i| i.id.clone()).collect();
        let Some(prev) = self.attention.replace(ids) else {
            return match items.len() {
                0 => vec![],
                1 => vec![attention_notice(&items[0])],
                n => vec![Notice {
                    title: "Rook".into(),
                    body: format!("{n} things need you."),
                }],
            };
        };
        let out = items
            .iter()
            .filter(|i| !prev.contains(&i.id))
            .map(attention_notice)
            .collect();
        summarize(out, "things need you")
    }
}

fn attention_notice(item: &AttentionItem) -> Notice {
    let title = match item.kind.as_str() {
        "ask" | "card" => "A question for you",
        "hold" | "job" => "An agent is waiting for your OK",
        "house" => "The house agent asks",
        "trust" => "A trust change to decide",
        _ => "Needs you",
    }
    .to_string();
    let body = match (item.title.trim(), item.text.trim()) {
        ("", t) => clip(t, 160),
        (h, "") => clip(h, 160),
        (h, t) => format!("{}: {}", clip(h, 60), clip(t, 120)),
    };
    Notice { title, body }
}

fn summarize(mut out: Vec<Notice>, what: &str) -> Vec<Notice> {
    if out.len() > MAX_SEPARATE {
        let n = out.len();
        out.clear();
        out.push(Notice {
            title: "Rook".into(),
            body: format!("{n} {what}."),
        });
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn item(id: &str, kind: &str) -> AttentionItem {
        AttentionItem {
            id: id.into(),
            kind: kind.into(),
            title: format!("T{id}"),
            text: "please look".into(),
        }
    }

    #[test]
    fn attention_items() {
        let mut w = Watcher::new();
        assert!(w.attention(None).is_empty());
        let first = w.attention(Some(&[item("1", "ask"), item("2", "hold")]));
        assert_eq!(
            first,
            vec![Notice {
                title: "Rook".into(),
                body: "2 things need you.".into()
            }]
        );
        let next = w.attention(Some(&[item("2", "hold"), item("3", "hold")]));
        assert_eq!(
            next,
            vec![Notice {
                title: "An agent is waiting for your OK".into(),
                body: "T3: please look".into()
            }]
        );
        let mut fresh = Watcher::new();
        assert_eq!(
            fresh.attention(Some(&[item("9", "card")]))[0].title,
            "A question for you"
        );
    }
}
