//! What deserves a desktop notification, decided from successive polls.
//!
//! Pure: the poller in lib.rs fetches, this decides. The first poll only
//! records where things stand (no burst of old messages at start-up); later
//! polls report what is new.

use crate::hub::{AttentionItem, Room, OPERATOR};
use std::collections::{HashMap, HashSet};

#[derive(Debug, Clone, PartialEq)]
pub struct Notice {
    pub title: String,
    pub body: String,
}

/// Most separate notifications from one poll; more become one summary.
const MAX_SEPARATE: usize = 3;

#[derive(Default)]
pub struct Watcher {
    unread: Option<HashMap<String, u64>>,
    attention: Option<HashSet<String>>,
}

fn display_name(identity: &str) -> String {
    let short = identity.split_once(':').map(|(_, n)| n).unwrap_or(identity);
    if short.is_empty() {
        "Someone".into()
    } else {
        short.to_string()
    }
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

    /// Rooms the operator is in whose unread count went up since the last
    /// poll, with the newest message from someone else. `skip` is the
    /// quick-ask room: its replies already show in the quick-ask box.
    pub fn rooms(&mut self, rooms: &[Room], skip: Option<&str>) -> Vec<Notice> {
        let now: HashMap<String, u64> = rooms
            .iter()
            .filter(|r| r.member)
            .map(|r| (r.room.clone(), r.unread))
            .collect();
        let Some(prev) = self.unread.replace(now) else {
            return vec![];
        };
        let mut out = vec![];
        for r in rooms.iter().filter(|r| r.member && Some(r.room.as_str()) != skip) {
            let before = prev.get(&r.room).copied().unwrap_or(0);
            let from = r.last_sender.as_deref().unwrap_or("");
            if r.unread <= before || from.is_empty() || from == OPERATOR {
                continue;
            }
            let text = r.last_text.as_deref().unwrap_or("");
            let who = display_name(from);
            let title = if text.trim_end().ends_with('?') {
                format!("{who} is asking")
            } else {
                format!("New message from {who}")
            };
            let room = if r.title.is_empty() {
                "Chat".to_string()
            } else {
                clip(&r.title, 40)
            };
            out.push(Notice {
                title,
                body: format!("{room}: {}", clip(text, 140)),
            });
        }
        summarize(out, "rooms have new messages")
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
        "question" => "A question for you".to_string(),
        "task" => "A task needs you".to_string(),
        _ => "Needs you".to_string(),
    };
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

    fn room(id: &str, unread: u64, from: &str, text: &str) -> Room {
        Room {
            room: id.into(),
            title: format!("room {id}"),
            participants: vec![OPERATOR.into(), from.into()],
            member: true,
            unread,
            last_sender: Some(from.into()),
            last_text: Some(text.into()),
        }
    }

    #[test]
    fn first_poll_is_a_baseline() {
        let mut w = Watcher::new();
        assert!(w.rooms(&[room("a", 5, "agent:x", "old")], None).is_empty());
        let got = w.rooms(&[room("a", 6, "agent:x", "Shall I deploy?")], None);
        assert_eq!(
            got,
            vec![Notice {
                title: "x is asking".into(),
                body: "room a: Shall I deploy?".into()
            }]
        );
        // Same count again: nothing new.
        assert!(w.rooms(&[room("a", 6, "agent:x", "Shall I deploy?")], None).is_empty());
    }

    #[test]
    fn own_messages_skipped_rooms_and_new_rooms() {
        let mut w = Watcher::new();
        w.rooms(&[], None);
        let mut mine = room("a", 1, OPERATOR, "hi");
        mine.unread = 1;
        let mut outsider = room("o", 3, "agent:y", "x");
        outsider.member = false;
        let got = w.rooms(
            &[
                mine,
                outsider,
                room("q", 1, "agent:home", "answer"),
                room("n", 1, "agent:z", "done"),
            ],
            Some("q"),
        );
        assert_eq!(got.len(), 1);
        assert_eq!(got[0].title, "New message from z");
    }

    #[test]
    fn many_become_a_summary_and_text_is_clipped() {
        let mut w = Watcher::new();
        w.rooms(&[], None);
        let rooms: Vec<Room> = (0..5).map(|i| room(&i.to_string(), 1, "agent:a", "hey")).collect();
        assert_eq!(
            w.rooms(&rooms, None),
            vec![Notice {
                title: "Rook".into(),
                body: "5 rooms have new messages.".into()
            }]
        );
        let long = "x".repeat(400);
        let mut w = Watcher::new();
        w.rooms(&[], None);
        let got = w.rooms(&[room("a", 1, "agent:a", &long)], None);
        assert!(got[0].body.chars().count() < 200);
        assert!(got[0].body.ends_with('\u{2026}'));
    }

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
        let first = w.attention(Some(&[item("1", "question"), item("2", "task")]));
        assert_eq!(
            first,
            vec![Notice {
                title: "Rook".into(),
                body: "2 things need you.".into()
            }]
        );
        let next = w.attention(Some(&[item("2", "task"), item("3", "task")]));
        assert_eq!(
            next,
            vec![Notice {
                title: "A task needs you".into(),
                body: "T3: please look".into()
            }]
        );
        let mut fresh = Watcher::new();
        assert_eq!(
            fresh.attention(Some(&[item("9", "question")]))[0].title,
            "A question for you"
        );
    }
}
