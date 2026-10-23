"""Which Whisper output counts as speech worth a turn.

Whisper invents text on silence, noise and echo ("Thank you.", "you", "Thanks
for watching!"). Faster-whisper's own VAD runs first (``vad_filter``); this
module then judges what is left, segment by segment and as a whole, and says
why a transcript was dropped so the turn log can count it. Pure functions: the
provider hands in plain segment fields, tests hand in dicts.
"""
import os
import re

# Per-segment gates (Whisper's own quality signals).
MAX_NO_SPEECH = float(os.environ.get('MAX_NO_SPEECH', '0.6'))         # no_speech_prob ceiling
MIN_LOGPROB = float(os.environ.get('MIN_LOGPROB', '-1.0'))            # avg_logprob floor
MAX_COMPRESSION = float(os.environ.get('MAX_COMPRESSION_RATIO', '2.4'))  # repetitive output
# Whole-utterance gates.
MIN_SPEECH_S = float(os.environ.get('VOICE_MIN_SPEECH_S', '0.25'))    # voiced audio after Whisper's VAD
# A short stock phrase is only believed when Whisper was sure of it.
BLOCKLIST_NO_SPEECH = float(os.environ.get('VOICE_BLOCKLIST_NO_SPEECH', '0.15'))
BLOCKLIST_LOGPROB = float(os.environ.get('VOICE_BLOCKLIST_LOGPROB', '-0.45'))
BLOCKLIST_MIN_SPEECH_S = float(os.environ.get('VOICE_BLOCKLIST_MIN_SPEECH_S', '0.6'))

# What Whisper says over silence or noise (trained on subtitles). Normalised:
# lower case, letters, digits and single spaces.
BLOCKLIST = frozenset({
    'thank you', 'thank you very much', 'thank you so much', 'thanks', 'thanks for watching',
    'thank you for watching', 'thanks for watching and see you next time', 'please subscribe',
    'subscribe', 'like and subscribe', 'you', 'bye', 'bye bye', 'goodbye', 'the end', 'so',
    'oh', 'hmm', 'mm', 'uh', 'um', 'ah', 'subtitles by the amaraorg community',
    'subtitles by', 'transcription by castingwords', 'i', 'okay', 'ok', 'yeah', 'music',
    'applause', 'silence', 'laughter', 'beep',
})


def normalise(text):
    text = re.sub(r'\[[^\]]*\]|\([^)]*\)|♪', ' ', str(text).lower())
    return ' '.join(re.sub(r'[^a-z0-9 ]+', '', text).split())


def judge(segments, speech_s=None):
    """``segments``: dicts with text, no_speech_prob, avg_logprob, compression_ratio.
    ``speech_s``: voiced seconds after VAD (None if unknown).
    Returns ``(text, reason)``: reason is None for a real transcript, else one of
    no_speech, too_short, low_confidence, repetitive, hallucination, empty."""
    if speech_s is not None and speech_s < MIN_SPEECH_S:
        return '', 'too_short' if segments else 'no_speech'
    if not segments:
        return '', 'no_speech'
    kept, reasons = [], []
    for seg in segments:
        text = str(seg.get('text', '')).strip()
        if not text:
            continue
        if seg.get('no_speech_prob', 0.0) > MAX_NO_SPEECH:
            reasons.append('no_speech')
        elif seg.get('avg_logprob', 0.0) < MIN_LOGPROB:
            reasons.append('low_confidence')
        elif seg.get('compression_ratio', 0.0) > MAX_COMPRESSION:
            reasons.append('repetitive')
        else:
            kept.append(seg)
    if not kept:
        return '', reasons[0] if reasons else 'empty'
    text = ' '.join(str(seg['text']).strip() for seg in kept).strip()
    words = normalise(text).split()
    if not words:
        return '', 'empty'
    if len(words) >= 4 and len(set(words)) == 1:
        return '', 'repetitive'
    if ' '.join(words) in BLOCKLIST:
        sure = (max(seg.get('no_speech_prob', 0.0) for seg in kept) <= BLOCKLIST_NO_SPEECH and
                min(seg.get('avg_logprob', 0.0) for seg in kept) >= BLOCKLIST_LOGPROB and
                (speech_s is None or speech_s >= BLOCKLIST_MIN_SPEECH_S))
        if not sure:
            return '', 'hallucination'
    return text, None


def stats(segments, speech_s=None):
    """Compact numbers for the turn log (no text)."""
    if not segments:
        return {'speech_s': round(speech_s, 2) if speech_s is not None else None}
    return {'speech_s': round(speech_s, 2) if speech_s is not None else None,
            'no_speech': round(max(s.get('no_speech_prob', 0.0) for s in segments), 3),
            'logprob': round(min(s.get('avg_logprob', 0.0) for s in segments), 3),
            'compression': round(max(s.get('compression_ratio', 0.0) for s in segments), 2)}
