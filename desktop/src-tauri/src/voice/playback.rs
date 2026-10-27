//! Playing speech (cpal): mono 16-bit PCM on the default output.

use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

/// Linear resampling of mono PCM to the output's rate.
pub fn resample(pcm: &[i16], from: u32, to: u32) -> Vec<f32> {
    let src: Vec<f32> = pcm.iter().map(|&s| s as f32 / 32768.0).collect();
    if from == to || src.is_empty() {
        return src;
    }
    let n = (src.len() as u64 * to as u64 / from as u64) as usize;
    let step = from as f64 / to as f64;
    (0..n)
        .map(|i| {
            let x = i as f64 * step;
            let j = x as usize;
            let frac = (x - j as f64) as f32;
            let a = src[j.min(src.len() - 1)];
            let b = src[(j + 1).min(src.len() - 1)];
            a + (b - a) * frac
        })
        .collect()
}

/// Play `pcm` (at `rate`) to the end, or until `stop` is set. Blocks the
/// calling thread (a cpal stream lives on one thread).
pub fn play(pcm: &[i16], rate: u32, stop: &AtomicBool) -> Result<(), String> {
    let device = cpal::default_host()
        .default_output_device()
        .ok_or("No speaker or headphones found.")?;
    let config = device
        .default_output_config()
        .map_err(|e| format!("The speaker can't be opened ({e})."))?;
    let channels = config.channels() as usize;
    let out_rate = config.sample_rate().0;
    let queue = Arc::new(Mutex::new(
        resample(pcm, rate, out_rate).into_iter().collect::<VecDeque<f32>>(),
    ));
    let q = queue.clone();
    let next = move || q.lock().unwrap().pop_front().unwrap_or(0.0);
    let err = |e| log::warn!("speaker stream error: {e}");
    let cfg: cpal::StreamConfig = config.clone().into();
    let stream = match config.sample_format() {
        cpal::SampleFormat::F32 => device.build_output_stream(
            &cfg,
            move |d: &mut [f32], _: &cpal::OutputCallbackInfo| {
                for frame in d.chunks_mut(channels) {
                    frame.fill(next());
                }
            },
            err,
            None,
        ),
        cpal::SampleFormat::I16 => device.build_output_stream(
            &cfg,
            move |d: &mut [i16], _: &cpal::OutputCallbackInfo| {
                for frame in d.chunks_mut(channels) {
                    frame.fill((next() * 32767.0) as i16);
                }
            },
            err,
            None,
        ),
        cpal::SampleFormat::U16 => device.build_output_stream(
            &cfg,
            move |d: &mut [u16], _: &cpal::OutputCallbackInfo| {
                for frame in d.chunks_mut(channels) {
                    frame.fill((next() * 32767.0 + 32768.0) as u16);
                }
            },
            err,
            None,
        ),
        other => return Err(format!("Unsupported speaker format {other:?}.")),
    }
    .map_err(|e| format!("The speaker can't be opened ({e})."))?;
    stream.play().map_err(|e| format!("The speaker can't start ({e})."))?;
    while !stop.load(Ordering::Relaxed) && !queue.lock().unwrap().is_empty() {
        std::thread::sleep(Duration::from_millis(30));
    }
    // Let the device drain its last buffer before the stream goes.
    if !stop.load(Ordering::Relaxed) {
        std::thread::sleep(Duration::from_millis(150));
    }
    drop(stream);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resampling_keeps_length_and_shape() {
        assert_eq!(resample(&[0, 16384], 24_000, 24_000), vec![0.0, 0.5]);
        assert_eq!(resample(&[0; 240], 24_000, 48_000).len(), 480);
        assert_eq!(resample(&[0; 480], 48_000, 44_100).len(), 441);
        let up = resample(&[0, 16384], 1, 2);
        assert_eq!(up, vec![0.0, 0.25, 0.5, 0.5]);
        assert!(resample(&[], 1, 2).is_empty());
    }
}
