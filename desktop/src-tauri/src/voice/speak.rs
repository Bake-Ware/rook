//! Reading an answer aloud: plain text in pieces the voice server takes
//! (`POST /api/voice`, `{"text"}` of 1 to 700 characters, a WAV back: mono
//! 16-bit PCM at the engine's rate).

use super::voice_base;
use std::time::Duration;

/// The voice server's limit per request (services/voice/pianobar_tts.py).
pub const MAX_CHARS: usize = 700;
/// Longer answers are cut here: the rest is on screen.
pub const MAX_TOTAL: usize = 2_400;

/// Markdown and links read badly aloud: drop the markup, keep the words.
pub fn speakable(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut in_code = false;
    for line in text.lines() {
        let t = line.trim();
        if t.starts_with("```") {
            in_code = !in_code;
            continue;
        }
        if in_code || t.starts_with('|') {
            continue; // code and tables: shown, not read
        }
        let t = t.trim_start_matches(['#', '>', '-', '*', '+', ' ']);
        let mut line = String::with_capacity(t.len());
        let mut rest = t;
        // [label](url) -> label
        while let Some(open) = rest.find('[') {
            let Some(close) = rest[open..].find("](").map(|i| open + i) else {
                break;
            };
            let Some(end) = rest[close..].find(')').map(|i| close + i) else {
                break;
            };
            line.push_str(&rest[..open]);
            line.push_str(&rest[open + 1..close]);
            rest = &rest[end + 1..];
        }
        line.push_str(rest);
        let line: String = line.chars().filter(|c| !matches!(c, '*' | '`' | '_')).collect();
        let words: Vec<&str> = line
            .split_whitespace()
            .filter(|w| !w.starts_with("http://") && !w.starts_with("https://"))
            .collect();
        if words.is_empty() {
            continue;
        }
        if !out.is_empty() {
            out.push(' ');
        }
        out.push_str(&words.join(" "));
    }
    out
}

/// The answer as pieces of at most `MAX_CHARS`, split at sentence ends
/// where it can, at most `MAX_TOTAL` characters in all.
pub fn chunks(text: &str) -> Vec<String> {
    let plain = speakable(text);
    let mut total = 0;
    let mut out: Vec<String> = vec![];
    let mut cur = String::new();
    for sentence in sentences(&plain) {
        if total + sentence.chars().count() > MAX_TOTAL {
            break;
        }
        total += sentence.chars().count() + 1;
        for piece in hard_split(&sentence) {
            if !cur.is_empty() && cur.chars().count() + 1 + piece.chars().count() > MAX_CHARS {
                out.push(std::mem::take(&mut cur));
            }
            if !cur.is_empty() {
                cur.push(' ');
            }
            cur.push_str(&piece);
        }
    }
    if !cur.is_empty() {
        out.push(cur);
    }
    out
}

fn sentences(text: &str) -> Vec<String> {
    let mut out = vec![];
    let mut cur = String::new();
    let mut chars = text.chars().peekable();
    while let Some(c) = chars.next() {
        cur.push(c);
        if matches!(c, '.' | '!' | '?') && chars.peek().map_or(true, |n| n.is_whitespace()) {
            out.push(cur.trim().to_string());
            cur.clear();
        }
    }
    if !cur.trim().is_empty() {
        out.push(cur.trim().to_string());
    }
    out
}

/// A sentence longer than the limit, cut at spaces.
fn hard_split(sentence: &str) -> Vec<String> {
    if sentence.chars().count() <= MAX_CHARS {
        return vec![sentence.to_string()];
    }
    let mut out = vec![];
    let mut cur = String::new();
    for word in sentence.split(' ') {
        let word: String = word.chars().take(MAX_CHARS).collect();
        if !cur.is_empty() && cur.chars().count() + 1 + word.chars().count() > MAX_CHARS {
            out.push(std::mem::take(&mut cur));
        }
        if !cur.is_empty() {
            cur.push(' ');
        }
        cur.push_str(&word);
    }
    if !cur.is_empty() {
        out.push(cur);
    }
    out
}

/// Mono 16-bit PCM and its rate from a WAV file (the voice server's reply).
pub fn parse_wav(bytes: &[u8]) -> Result<(Vec<i16>, u32), String> {
    let bad = || "The voice server sent audio the app can't play.".to_string();
    if bytes.len() < 12 || &bytes[0..4] != b"RIFF" || &bytes[8..12] != b"WAVE" {
        return Err(bad());
    }
    let (mut rate, mut channels, mut bits) = (0u32, 0u16, 0u16);
    let mut at = 12;
    while at + 8 <= bytes.len() {
        let id = &bytes[at..at + 4];
        let size = u32::from_le_bytes(bytes[at + 4..at + 8].try_into().unwrap()) as usize;
        let body = at + 8;
        let end = body.saturating_add(size).min(bytes.len());
        if id == b"fmt " && end - body >= 16 {
            let f = &bytes[body..end];
            channels = u16::from_le_bytes([f[2], f[3]]);
            rate = u32::from_le_bytes([f[4], f[5], f[6], f[7]]);
            bits = u16::from_le_bytes([f[14], f[15]]);
        } else if id == b"data" {
            if bits != 16 || channels == 0 || !(8_000..=192_000).contains(&rate) {
                return Err(bad());
            }
            let frame = 2 * channels as usize;
            let pcm = bytes[body..end]
                .chunks_exact(frame)
                .map(|f| i16::from_le_bytes([f[0], f[1]]))
                .collect();
            return Ok((pcm, rate));
        }
        at = body + size + (size & 1);
    }
    Err(bad())
}

/// One piece of speech from the voice server.
pub async fn synthesize(voice_url: &str, key: &str, text: &str) -> Result<(Vec<i16>, u32), String> {
    let base = voice_base(voice_url)?;
    let http = reqwest::Client::builder()
        .timeout(Duration::from_secs(40))
        .build()
        .map_err(|e| e.to_string())?;
    let mut req = http
        .post(format!("{base}/api/voice"))
        .json(&serde_json::json!({ "text": text }));
    if !key.is_empty() {
        req = req.bearer_auth(key);
    }
    let resp = req
        .send()
        .await
        .map_err(|e| format!("Can't reach the voice server ({e})."))?;
    match resp.status().as_u16() {
        200 => {}
        401 => return Err("The voice server wants a valid voice key (Settings > Voice).".into()),
        404 => return Err("This voice server can't speak answers (it has no /api/voice).".into()),
        503 => return Err("The voice server is busy, so the answer is shown, not read.".into()),
        s => return Err(format!("The voice server answered HTTP {s}.")),
    }
    let bytes = resp
        .bytes()
        .await
        .map_err(|_| "The voice server's audio didn't arrive.".to_string())?;
    parse_wav(&bytes)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn markup_is_dropped() {
        let md = "## Done\n\n- **Merged** as `abc123`, see [the PR](https://github.com/x/y/pull/1).\n\
                  ```\ncargo test\n```\n| a | b |\nLink: https://example.com ok";
        assert_eq!(speakable(md), "Done Merged as abc123, see the PR. Link: ok");
    }

    #[test]
    fn chunks_respect_the_limits() {
        assert!(chunks("").is_empty());
        assert_eq!(chunks("One. Two? Three!"), vec!["One. Two? Three!"]);
        let long = "word ".repeat(400); // one 2,000-character sentence
        let c = chunks(&long);
        assert!(c.len() >= 3);
        assert!(c.iter().all(|p| !p.is_empty() && p.chars().count() <= MAX_CHARS));
        let many = "This is a sentence of some length. ".repeat(200);
        let c = chunks(&many);
        let total: usize = c.iter().map(|p| p.chars().count()).sum();
        assert!(total <= MAX_TOTAL, "{total}");
        assert!(c.iter().all(|p| p.ends_with('.')));
        // Version numbers aren't sentence ends.
        assert_eq!(
            sentences("Ship 0.2.0 now. Then rest."),
            vec!["Ship 0.2.0 now.", "Then rest."]
        );
    }

    fn wav(rate: u32, channels: u16, samples: &[i16]) -> Vec<u8> {
        let data: Vec<u8> = samples.iter().flat_map(|s| s.to_le_bytes()).collect();
        let mut b = b"RIFF".to_vec();
        b.extend((36 + data.len() as u32).to_le_bytes());
        b.extend(b"WAVEfmt ");
        b.extend(16u32.to_le_bytes());
        b.extend(1u16.to_le_bytes());
        b.extend(channels.to_le_bytes());
        b.extend(rate.to_le_bytes());
        b.extend((rate * 2 * channels as u32).to_le_bytes());
        b.extend((2 * channels).to_le_bytes());
        b.extend(16u16.to_le_bytes());
        b.extend(b"data");
        b.extend((data.len() as u32).to_le_bytes());
        b.extend(data);
        b
    }

    #[test]
    fn wav_is_read() {
        assert_eq!(
            parse_wav(&wav(24_000, 1, &[1, -2, 3])).unwrap(),
            (vec![1, -2, 3], 24_000)
        );
        // Stereo: the first channel.
        assert_eq!(parse_wav(&wav(22_050, 2, &[5, 6, 7, 8])).unwrap(), (vec![5, 7], 22_050));
        assert!(parse_wav(b"RIFF....WAVEjunk").is_err());
        assert!(parse_wav(&wav(4_000, 1, &[1])).is_err());
        assert!(parse_wav(b"not audio").is_err());
    }
}
