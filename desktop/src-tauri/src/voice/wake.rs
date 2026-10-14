//! Wake word: openWakeWord's streaming pipeline on tract (pure Rust ONNX).
//!
//! A port of the Android app's WakeWordDetector (itself a port of
//! openwakeword/utils.py AudioFeatures):
//!
//!   16 kHz PCM --(1280-sample chunks; melspectrogram over the last 1760)-->
//!   mel frames (x/10+2, 32 bins) --(last 76 frames, [1,76,32,1])-->
//!   96-d embedding --(last 16, [1,16,96])--> wake score in [0,1]
//!
//! melspectrogram.onnx and embedding_model.onnx are openWakeWord's shared
//! feature models (Apache-2.0; the embedding model is Google's
//! speech_embedding). They ship with the app. The wake classifier is the
//! user's own `.onnx`: openWakeWord's pre-trained classifiers are licensed
//! CC BY-NC-SA 4.0, so none is bundled (the phone app does the same).

use super::{capture, Endpoint, Endpointer};
use std::collections::VecDeque;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread::JoinHandle;
use tract_onnx::prelude::*;

pub const CHUNK: usize = 1280; // 80 ms
const MEL_CTX: usize = 160 * 3;
const WINDOW: usize = CHUNK + MEL_CTX;
const MEL_BINS: usize = 32;
const MEL_MAX: usize = 970;
const EMB_WIN: usize = 76;
const EMB_DIM: usize = 96;
const EMB_MAX: usize = 120;
const N_FRAMES: usize = 16;
/// A chunk this loud (RMS of int16) counts as someone speaking. The phone
/// gates on Silero VAD; a loudness gate keeps this build dependency-free.
const SPEECH_RMS: f32 = 400.0;

type Plan = TypedRunnableModel<TypedModel>;

fn load(path: &Path, shape: &[usize]) -> Result<Plan, String> {
    let name = path.display().to_string();
    (|| -> TractResult<Plan> {
        tract_onnx::onnx()
            .model_for_path(path)?
            .with_input_fact(0, f32::fact(shape).into())?
            .into_optimized()?
            .into_runnable()
    })()
    .map_err(|e| format!("Can't load the wake model {name}: {e}"))
}

fn run(plan: &Plan, shape: &[usize], data: Vec<f32>) -> Result<Vec<f32>, String> {
    let t = tract_ndarray::ArrayD::from_shape_vec(shape, data).map_err(|e| e.to_string())?;
    let out = plan.run(tvec!(Tensor::from(t).into())).map_err(|e| e.to_string())?;
    let view = out[0].to_array_view::<f32>().map_err(|e| e.to_string())?;
    Ok(view.iter().copied().collect())
}

pub struct Models {
    pub features_dir: PathBuf,
    pub classifier: PathBuf,
}

pub struct Detector {
    mel: Plan,
    emb: Plan,
    wake: Plan,
    raw: Vec<f32>,
    pending: Vec<f32>,
    mel_frames: VecDeque<[f32; MEL_BINS]>,
    embs: VecDeque<Vec<f32>>,
    confirmations: u32,
    pub threshold: f32,
    pub last_score: f32,
}

impl Detector {
    pub fn new(models: &Models, threshold: f32) -> Result<Self, String> {
        if !models.classifier.is_file() {
            return Err("Choose your wake word model (.onnx) in Settings > Voice.".into());
        }
        Ok(Detector {
            mel: load(&models.features_dir.join("melspectrogram.onnx"), &[1, WINDOW])?,
            emb: load(
                &models.features_dir.join("embedding_model.onnx"),
                &[1, EMB_WIN, MEL_BINS, 1],
            )?,
            wake: load(&models.classifier, &[1, N_FRAMES, EMB_DIM])?,
            raw: vec![0.0; WINDOW],
            pending: Vec::with_capacity(CHUNK),
            mel_frames: VecDeque::new(),
            embs: VecDeque::new(),
            confirmations: 0,
            threshold,
            last_score: 0.0,
        })
    }

    /// Feed 16 kHz PCM (any length). True once the wake word is heard: a
    /// score at or above the threshold in two chunks in a row, with speech.
    pub fn feed(&mut self, pcm: &[i16]) -> Result<bool, String> {
        let mut hit = false;
        for &s in pcm {
            self.pending.push(s as f32);
            if self.pending.len() == CHUNK {
                let chunk = std::mem::take(&mut self.pending);
                if self.process(&chunk)? {
                    hit = true;
                }
                self.pending = chunk;
                self.pending.clear();
            }
        }
        Ok(hit)
    }

    fn process(&mut self, chunk: &[f32]) -> Result<bool, String> {
        let speech = (chunk.iter().map(|s| s * s).sum::<f32>() / chunk.len() as f32).sqrt() > SPEECH_RMS;
        self.raw.drain(..CHUNK);
        self.raw.extend_from_slice(chunk);
        let flat = run(&self.mel, &[1, WINDOW], self.raw.clone())?;
        for frame in flat.chunks_exact(MEL_BINS) {
            let mut row = [0f32; MEL_BINS];
            for (r, v) in row.iter_mut().zip(frame) {
                *r = v / 10.0 + 2.0;
            }
            self.mel_frames.push_back(row);
        }
        while self.mel_frames.len() > MEL_MAX {
            self.mel_frames.pop_front();
        }
        if self.mel_frames.len() < EMB_WIN {
            return Ok(false);
        }
        let win: Vec<f32> = self
            .mel_frames
            .iter()
            .skip(self.mel_frames.len() - EMB_WIN)
            .flatten()
            .copied()
            .collect();
        let mut e = run(&self.emb, &[1, EMB_WIN, MEL_BINS, 1], win)?;
        e.truncate(EMB_DIM);
        self.embs.push_back(e);
        while self.embs.len() > EMB_MAX {
            self.embs.pop_front();
        }
        if self.embs.len() < N_FRAMES {
            return Ok(false);
        }
        let x: Vec<f32> = self
            .embs
            .iter()
            .skip(self.embs.len() - N_FRAMES)
            .flatten()
            .copied()
            .collect();
        self.last_score = run(&self.wake, &[1, N_FRAMES, EMB_DIM], x)?
            .first()
            .copied()
            .unwrap_or(0.0);
        self.confirmations = if self.last_score >= self.threshold && speech {
            self.confirmations + 1
        } else {
            0
        };
        Ok(self.confirmations >= 2)
    }

    /// Forget recent audio (after a detection, so its tail can't re-fire).
    pub fn reset(&mut self) {
        self.raw.iter_mut().for_each(|v| *v = 0.0);
        self.pending.clear();
        self.mel_frames.clear();
        self.embs.clear();
        self.confirmations = 0;
        self.last_score = 0.0;
    }

    #[cfg(test)]
    fn embedding(&self, frames: Vec<f32>) -> Result<Vec<f32>, String> {
        run(&self.emb, &[1, EMB_WIN, MEL_BINS, 1], frames)
    }

    #[cfg(test)]
    fn mel(&self, samples: Vec<f32>) -> Result<Vec<f32>, String> {
        run(&self.mel, &[1, WINDOW], samples)
    }
}

pub enum Event {
    /// The wake word was heard; listening for the question.
    Woke,
    /// What was said after it (16 kHz PCM); empty when nothing was.
    Utterance(Vec<i16>),
    Error(String),
}

/// The always-on listener: wake word, then the question until a pause.
pub struct Listener {
    stop: Arc<AtomicBool>,
    handle: Option<JoinHandle<()>>,
}

impl Listener {
    pub fn start(models: Models, threshold: f32, on_event: impl Fn(Event) + Send + 'static) -> Result<Self, String> {
        // Load on the caller's thread, so a bad model is reported right away.
        let mut det = Detector::new(&models, threshold)?;
        let stop = Arc::new(AtomicBool::new(false));
        let flag = stop.clone();
        let handle = std::thread::spawn(move || {
            let mut question: Option<(Endpointer, Vec<i16>)> = None;
            let res = capture::run(&flag, &mut |pcm| {
                if let Some((ep, buf)) = question.as_mut() {
                    buf.extend_from_slice(pcm);
                    match ep.feed(pcm) {
                        Endpoint::Listening => {}
                        Endpoint::Done => {
                            on_event(Event::Utterance(std::mem::take(buf)));
                            question = None;
                            det.reset();
                        }
                        Endpoint::Nothing => {
                            on_event(Event::Utterance(vec![]));
                            question = None;
                            det.reset();
                        }
                    }
                    return true;
                }
                match det.feed(pcm) {
                    Ok(true) => {
                        on_event(Event::Woke);
                        det.reset();
                        question = Some((Endpointer::default(), Vec::new()));
                    }
                    Ok(false) => {}
                    Err(e) => {
                        on_event(Event::Error(e));
                        return false;
                    }
                }
                true
            });
            if let Err(e) = res {
                on_event(Event::Error(e));
            }
        });
        Ok(Listener {
            stop,
            handle: Some(handle),
        })
    }

    pub fn stop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(h) = self.handle.take() {
            let _ = h.join();
        }
    }
}

impl Drop for Listener {
    fn drop(&mut self) {
        self.stop();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn models() -> Models {
        let root = Path::new(env!("CARGO_MANIFEST_DIR"));
        Models {
            features_dir: root.join("../../android/app/src/main/assets/wakeword"),
            classifier: root.join("tests/fixtures/mean_sigmoid.onnx"),
        }
    }

    #[test]
    fn feature_models_match_onnxruntime() {
        let d = Detector::new(&models(), 0.5).unwrap();
        // References computed with onnxruntime 1.31 on the same inputs.
        let x: Vec<f32> = (0..WINDOW).map(|i| (i as f32 * 0.05).sin() * 3000.0).collect();
        let mel = d.mel(x).unwrap();
        assert_eq!(mel.len(), 8 * MEL_BINS);
        for (got, want) in mel.iter().zip([92.8184f32, 84.93735, 54.94159, 42.12007]) {
            assert!((got - want).abs() < 1e-2, "mel {got} vs {want}");
        }
        let frames: Vec<f32> = (0..EMB_WIN * MEL_BINS).map(|i| (i % 7) as f32 / 3.0).collect();
        let emb = d.embedding(frames).unwrap();
        assert_eq!(emb.len(), EMB_DIM);
        for (got, want) in emb.iter().zip([-6.812254f32, 25.904556, 7.4553995]) {
            assert!((got - want).abs() < 1e-2, "emb {got} vs {want}");
        }
    }

    #[test]
    fn streaming_pipeline_scores_and_fires() {
        // The fixture classifier is sigmoid(mean(embeddings)): not a wake
        // word, but it exercises the whole stream and the confirmation rule.
        let mut d = Detector::new(&models(), 0.0).unwrap();
        let loud: Vec<i16> = (0..16_000 * 3)
            .map(|i| ((i as f32 * 0.07).sin() * 9000.0) as i16)
            .collect();
        let mut fired = false;
        for c in loud.chunks(700) {
            fired |= d.feed(c).unwrap();
        }
        assert!(fired, "threshold 0 with speech fires after two confirmations");
        assert!(d.last_score > 0.0 && d.last_score < 1.0);
        d.reset();
        let quiet = vec![0i16; 16_000 * 3];
        assert!(!d.feed(&quiet).unwrap(), "no speech, no wake");
    }

    #[test]
    fn missing_classifier_is_a_plain_error() {
        let mut m = models();
        m.classifier = PathBuf::from("/nonexistent/hey.onnx");
        assert!(Detector::new(&m, 0.5).err().unwrap().contains("Settings"));
    }
}
