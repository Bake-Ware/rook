//! Microphone capture (cpal): the default input as 16 kHz mono PCM.

use super::{downmix, to_i16, Resampler, MAX_SECONDS, RATE};
use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc};
use std::thread::JoinHandle;
use std::time::Duration;

pub fn input_name() -> Option<String> {
    cpal::default_host().default_input_device().and_then(|d| d.name().ok())
}

/// Capture from the default input until `stop` is set or `sink` returns
/// false. Blocks the calling thread: cpal streams are not `Send` on every
/// platform, so a stream lives and dies on one thread.
pub fn run(stop: &AtomicBool, sink: &mut dyn FnMut(&[i16]) -> bool) -> Result<(), String> {
    let device = cpal::default_host()
        .default_input_device()
        .ok_or("No microphone found.")?;
    let config = device
        .default_input_config()
        .map_err(|e| format!("The microphone can't be opened ({e})."))?;
    let channels = config.channels() as usize;
    let rate = config.sample_rate().0;
    let (tx, rx) = mpsc::sync_channel::<Vec<f32>>(64);
    let err = |e| log::warn!("microphone stream error: {e}");
    let stream_cfg: cpal::StreamConfig = config.clone().into();
    let stream = match config.sample_format() {
        cpal::SampleFormat::F32 => device.build_input_stream(
            &stream_cfg,
            move |d: &[f32], _: &cpal::InputCallbackInfo| {
                let _ = tx.try_send(downmix(d, channels));
            },
            err,
            None,
        ),
        cpal::SampleFormat::I16 => device.build_input_stream(
            &stream_cfg,
            move |d: &[i16], _: &cpal::InputCallbackInfo| {
                let f: Vec<f32> = d.iter().map(|&s| s as f32 / 32768.0).collect();
                let _ = tx.try_send(downmix(&f, channels));
            },
            err,
            None,
        ),
        cpal::SampleFormat::U16 => device.build_input_stream(
            &stream_cfg,
            move |d: &[u16], _: &cpal::InputCallbackInfo| {
                let f: Vec<f32> = d.iter().map(|&s| (s as f32 - 32768.0) / 32768.0).collect();
                let _ = tx.try_send(downmix(&f, channels));
            },
            err,
            None,
        ),
        other => return Err(format!("Unsupported microphone format {other:?}.")),
    }
    .map_err(|e| format!("The microphone can't be opened ({e})."))?;
    stream
        .play()
        .map_err(|e| format!("The microphone can't start ({e})."))?;
    let mut rs = Resampler::new(rate);
    while !stop.load(Ordering::Relaxed) {
        match rx.recv_timeout(Duration::from_millis(50)) {
            Ok(chunk) => {
                if !sink(&to_i16(&rs.process(&chunk))) {
                    break;
                }
            }
            Err(mpsc::RecvTimeoutError::Timeout) => {}
            Err(mpsc::RecvTimeoutError::Disconnected) => return Err("The microphone stopped.".into()),
        }
    }
    drop(stream);
    Ok(())
}

/// A push-to-talk recording on its own thread.
pub struct Recording {
    stop: Arc<AtomicBool>,
    handle: JoinHandle<Result<Vec<i16>, String>>,
}

impl Recording {
    pub fn start() -> Self {
        let stop = Arc::new(AtomicBool::new(false));
        let flag = stop.clone();
        let handle = std::thread::spawn(move || {
            let mut buf: Vec<i16> = Vec::with_capacity(RATE as usize * 5);
            let max = MAX_SECONDS * RATE as usize;
            run(&flag, &mut |c| {
                buf.extend_from_slice(c);
                buf.len() < max
            })?;
            Ok(buf)
        });
        Recording { stop, handle }
    }

    /// The capture thread already ended: the microphone failed to open.
    pub fn ended(&self) -> bool {
        self.handle.is_finished()
    }

    pub fn finish(self) -> Result<Vec<i16>, String> {
        self.stop.store(true, Ordering::Relaxed);
        self.handle.join().map_err(|_| "The recording failed.".to_string())?
    }
}
