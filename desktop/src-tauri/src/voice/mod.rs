//! Voice: push-to-talk and (behind the `wake-word` feature) a wake word.
//!
//! The desktop owns its microphone outright, so none of the Android app's
//! mic arbitration is needed here (docs/design/desktop-voice.md). Speech goes
//! to the voice server's `/api/transcribe` (the same endpoint the phone uses
//! for spoken replies), and the text goes to the home agent like a typed
//! quick ask.
//!
//! This file is the platform-free part (resampling, end-of-speech detection,
//! the transcription request), always compiled and unit-tested. `capture`
//! needs the `voice` feature (cpal); `wake` needs `wake-word` (tract).

#[cfg(feature = "voice")]
pub mod capture;
#[cfg(feature = "wake-word")]
pub mod wake;

use std::time::Duration;

pub const RATE: u32 = 16_000;
/// The voice server takes at most 30 s per request.
pub const MAX_SECONDS: usize = 30;

/// Average interleaved channels into one.
pub fn downmix(interleaved: &[f32], channels: usize) -> Vec<f32> {
    if channels <= 1 {
        return interleaved.to_vec();
    }
    interleaved
        .chunks(channels)
        .map(|f| f.iter().sum::<f32>() / f.len() as f32)
        .collect()
}

/// Streaming linear resampler to 16 kHz. Good enough for speech recognition
/// and wake word features (both look at < 8 kHz content).
pub struct Resampler {
    step: f64,
    pos: f64,
    prev: f32,
}

impl Resampler {
    pub fn new(input_rate: u32) -> Self {
        Resampler {
            step: input_rate as f64 / RATE as f64,
            pos: 0.0,
            prev: 0.0,
        }
    }

    pub fn process(&mut self, input: &[f32]) -> Vec<f32> {
        let mut out = Vec::with_capacity((input.len() as f64 / self.step) as usize + 1);
        // Positions are relative to `prev` (index -1) followed by `input`.
        while self.pos < input.len() as f64 {
            let i = self.pos.floor() as isize;
            let frac = (self.pos - i as f64) as f32;
            let a = if i < 0 { self.prev } else { input[i as usize] };
            let b = if i + 1 < input.len() as isize {
                input[(i + 1) as usize]
            } else {
                a
            };
            out.push(a + (b - a) * frac);
            self.pos += self.step;
        }
        self.pos -= input.len() as f64;
        if let Some(&last) = input.last() {
            self.prev = last;
        }
        out
    }
}

pub fn to_i16(samples: &[f32]) -> Vec<i16> {
    samples.iter().map(|s| (s.clamp(-1.0, 1.0) * 32767.0) as i16).collect()
}

pub fn pcm_bytes(samples: &[i16]) -> Vec<u8> {
    samples.iter().flat_map(|s| s.to_le_bytes()).collect()
}

/// End-of-utterance detection by loudness: speech is a 30 ms frame well above
/// the running noise floor; the utterance ends after a pause.
pub struct Endpointer {
    noise: f32,
    heard_ms: u32,
    quiet_ms: u32,
    total_ms: u32,
    pending: Vec<i16>,
    pub pause_ms: u32,
    pub max_ms: u32,
    pub wait_ms: u32,
}

#[derive(Debug, PartialEq, Eq)]
pub enum Endpoint {
    Listening,
    /// Enough speech followed by a pause.
    Done,
    /// Nothing said before `wait_ms`.
    Nothing,
}

const FRAME: usize = 480; // 30 ms at 16 kHz

impl Default for Endpointer {
    fn default() -> Self {
        Endpointer {
            noise: 300.0,
            heard_ms: 0,
            quiet_ms: 0,
            total_ms: 0,
            pending: vec![],
            pause_ms: 900,
            max_ms: 15_000,
            wait_ms: 5_000,
        }
    }
}

impl Endpointer {
    pub fn feed(&mut self, samples: &[i16]) -> Endpoint {
        self.pending.extend_from_slice(samples);
        let mut state = Endpoint::Listening;
        let frames: Vec<Vec<i16>> = self.pending.chunks_exact(FRAME).map(|c| c.to_vec()).collect();
        let used = frames.len() * FRAME;
        self.pending.drain(..used);
        for f in frames {
            let rms = (f.iter().map(|&s| (s as f32) * (s as f32)).sum::<f32>() / FRAME as f32).sqrt();
            let speech = rms > (self.noise * 3.0).max(500.0);
            if !speech {
                // The floor follows quiet frames only, so speech can't raise it.
                self.noise = self.noise * 0.95 + rms * 0.05;
            }
            self.total_ms += 30;
            if speech {
                self.heard_ms += 30;
                self.quiet_ms = 0;
            } else if self.heard_ms > 0 {
                self.quiet_ms += 30;
            }
            if self.heard_ms >= 240 && self.quiet_ms >= self.pause_ms {
                state = Endpoint::Done;
            } else if self.total_ms >= self.max_ms {
                state = if self.heard_ms >= 240 {
                    Endpoint::Done
                } else {
                    Endpoint::Nothing
                };
            } else if self.total_ms >= self.wait_ms
                && (self.heard_ms == 0 || (self.heard_ms < 240 && self.quiet_ms >= self.pause_ms))
            {
                // Nothing said, or only a click or a cough.
                state = Endpoint::Nothing;
            }
            if state != Endpoint::Listening {
                break;
            }
        }
        state
    }
}

/// The voice server's HTTP base from what the person typed or what the
/// phone uses (`wss://voice.example.com/ws`).
pub fn voice_base(url: &str) -> Result<String, String> {
    let t = url.trim();
    if t.is_empty() {
        return Err("Add your voice server's address in Settings to use voice.".into());
    }
    let t = if let Some(r) = t.strip_prefix("wss://") {
        format!("https://{r}")
    } else if let Some(r) = t.strip_prefix("ws://") {
        format!("http://{r}")
    } else if t.contains("://") {
        t.to_string()
    } else {
        format!("https://{t}")
    };
    let mut u = url::Url::parse(&t).map_err(|_| "The voice server address isn't valid.".to_string())?;
    if !matches!(u.scheme(), "http" | "https") {
        return Err("The voice server address isn't valid.".into());
    }
    let path = u.path().trim_end_matches('/').trim_end_matches("/ws").to_string();
    u.set_path(&path);
    u.set_query(None);
    Ok(u.as_str().trim_end_matches('/').to_string())
}

/// Speech to text on the voice server (`POST /api/transcribe`, 16 kHz mono
/// PCM). Empty text means nothing intelligible was said.
pub async fn transcribe(voice_url: &str, key: &str, pcm: &[i16]) -> Result<String, String> {
    let max = MAX_SECONDS * RATE as usize;
    let pcm = if pcm.len() > max { &pcm[pcm.len() - max..] } else { pcm };
    if pcm.is_empty() {
        return Ok(String::new());
    }
    let base = voice_base(voice_url)?;
    let http = reqwest::Client::builder()
        .timeout(Duration::from_secs(40))
        .build()
        .map_err(|e| e.to_string())?;
    let mut req = http
        .post(format!("{base}/api/transcribe"))
        .header("Content-Type", "audio/L16")
        .body(pcm_bytes(pcm));
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
        503 => return Err("The voice server is busy. Try again.".into()),
        s => return Err(format!("The voice server answered HTTP {s}.")),
    }
    let v: serde_json::Value = resp
        .json()
        .await
        .map_err(|_| "The voice server sent a bad reply.".to_string())?;
    Ok(v.get("text").and_then(|t| t.as_str()).unwrap_or("").trim().to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn downmix_averages() {
        assert_eq!(downmix(&[1.0, 0.0, 0.5, 0.5], 2), vec![0.5, 0.5]);
        assert_eq!(downmix(&[0.1, 0.2], 1), vec![0.1, 0.2]);
    }

    #[test]
    fn resampler_rates() {
        let mut r = Resampler::new(48_000);
        let input: Vec<f32> = (0..4800).map(|i| i as f32).collect();
        let a = r.process(&input[..2400]);
        let b = r.process(&input[2400..]);
        assert_eq!(a.len() + b.len(), 1600);
        // A ramp stays a ramp across the chunk boundary.
        let all: Vec<f32> = a.into_iter().chain(b).collect();
        for w in all.windows(2) {
            assert!((w[1] - w[0] - 3.0).abs() < 1e-3, "{w:?}");
        }
        let mut same = Resampler::new(16_000);
        assert_eq!(same.process(&[0.1, 0.2, 0.3]), vec![0.1, 0.2, 0.3]);
        let mut up = Resampler::new(8_000);
        assert_eq!(up.process(&[0.0; 80]).len(), 160);
    }

    #[test]
    fn i16_conversion_clamps() {
        assert_eq!(to_i16(&[0.0, 1.5, -2.0]), vec![0, 32767, -32767]);
        assert_eq!(pcm_bytes(&[1, -2]), vec![1, 0, 0xfe, 0xff]);
    }

    fn tone(ms: usize, amp: f32) -> Vec<i16> {
        (0..ms * 16).map(|i| ((i as f32 * 0.3).sin() * amp) as i16).collect()
    }

    #[test]
    fn endpointer_ends_after_a_pause() {
        let mut e = Endpointer::default();
        assert_eq!(e.feed(&tone(600, 40.0)), Endpoint::Listening); // room noise
        assert_eq!(e.feed(&tone(800, 8000.0)), Endpoint::Listening); // speech
        assert_eq!(e.feed(&tone(500, 40.0)), Endpoint::Listening); // short pause
        assert_eq!(e.feed(&tone(500, 40.0)), Endpoint::Done);
    }

    #[test]
    fn endpointer_gives_up_on_silence_and_caps_length() {
        let mut e = Endpointer::default();
        assert_eq!(e.feed(&tone(5_100, 40.0)), Endpoint::Nothing);
        let mut e = Endpointer {
            max_ms: 2_000,
            ..Default::default()
        };
        assert_eq!(e.feed(&tone(2_100, 8000.0)), Endpoint::Done);
        // A click is not speech.
        let mut e = Endpointer::default();
        e.feed(&tone(60, 8000.0));
        assert_eq!(e.feed(&tone(5_000, 40.0)), Endpoint::Nothing);
    }

    #[test]
    fn voice_base_forms() {
        assert_eq!(
            voice_base("wss://voice.example.com/ws").unwrap(),
            "https://voice.example.com"
        );
        assert_eq!(voice_base("ws://192.0.2.5:8900/ws/").unwrap(), "http://192.0.2.5:8900");
        assert_eq!(voice_base("voice.example.com").unwrap(), "https://voice.example.com");
        assert_eq!(
            voice_base("https://example.com/voice/").unwrap(),
            "https://example.com/voice"
        );
        assert!(voice_base("").is_err());
        assert!(voice_base("ftp://x").is_err());
    }
}
