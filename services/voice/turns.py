"""Pipecat Smart Turn v3.2 inference, without installing unused media transports."""
import numpy as np
import onnxruntime as ort
from .vendor.whisper_features import compute_whisper_log_mel_features


class SmartTurn:
    def __init__(self, path):
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(path), sess_options=options, providers=['CPUExecutionProvider'])

    def complete(self, pcm):
        audio = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768
        audio = audio[-128000:]
        audio = np.pad(audio, (128000 - len(audio), 0))
        features = compute_whisper_log_mel_features(audio, do_normalize=True)[None, ...]
        score = float(self.session.run(None, {'input_features': features})[0][0].item())
        return score > .5
