"""Per-turn stage timing, measured from the end of the user's speech.

One structured log line per turn (logger ``voice.timing``) and the same fields
in the activity ``done`` event. Durations are integer milliseconds. Voice turns
measure from the last voiced microphone frame, so ``endpoint_ms`` is the time
spent deciding the utterance was over; typed and internal turns measure from
the moment the turn started (``endpoint_ms`` is then 0).
"""
import json
import logging
import os
import sys
import time

FIELDS = ('endpoint_ms', 'stt_ms', 'plan_ms', 'llm_ms', 'first_text_ms', 'first_audio_ms', 'total_ms',
          # front_background pipeline only: Front's first streamed token and first
          # audio packet, Background's completion, and the follow-up's first audio.
          'front_first_token_ms', 'front_first_audio_ms', 'background_ms', 'followup_ms')

log = logging.getLogger('voice.timing')
if os.environ.get('VOICE_TIMING_LOG', '1') != '0' and not log.handlers:
    # The service runs with warning-level logging; timing lines are info and
    # must still reach the journal, so this logger has its own handler.
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter('%(message)s'))
    log.addHandler(_handler)
    log.setLevel(logging.INFO)
    log.propagate = False


def _ms(seconds):
    return max(0, int(round(seconds * 1000)))


class TurnTiming:
    def __init__(self, turn, source, started, speech_end=None):
        self.turn, self.source = turn, source
        # A stale or future speech mark (typed turn, clock edge) falls back to the turn start.
        self.t0 = speech_end if speech_end is not None and speech_end <= started else started
        self.marks = {'endpoint_ms': _ms(started - self.t0)}

    def mark(self, name, now=None):
        """Record the first time a stage was reached, from end of speech."""
        if name not in self.marks:
            self.marks[name] = _ms((now if now is not None else time.monotonic()) - self.t0)

    def span(self, name, start, end=None):
        """Record a stage's own duration (e.g. STT, the planner call)."""
        if name not in self.marks:
            self.marks[name] = _ms((end if end is not None else time.monotonic()) - start)

    def fields(self):
        return {'source': self.source, **{k: self.marks[k] for k in FIELDS if k in self.marks}}

    def finish(self, status, now=None, emit=True):
        """Close the spoken turn. ``emit=False`` defers the log line to a later
        :meth:`log` (front_background: once Background and its follow-up finish)."""
        self.mark('total_ms', now)
        fields = self.fields()
        if emit:
            self.log(status)
        return fields

    def log(self, status):
        fields = self.fields()
        log.info(json.dumps({'event': 'voice_turn_timing', 'turn': self.turn, 'status': status, **fields},
                            separators=(',', ':')))
        return fields
