"""Turn intake (transcribe before interrupting, drop noise, merge, one turn at a
time), confirmed barge-in hold/resume, heard-text truncation, Whisper's gate and
Front's append-only transcript."""
import asyncio
import json
import logging
import re
import time

import pytest

pytest.importorskip("numpy")

from services.voice import front as front_mod
from services.voice import runtime as runtime_mod
from services.voice import stt_gate
from services.voice.board import Board
from services.voice.identity import Identity
from services.voice.jobs import Jobs
from services.voice.modes import resolve as mode
from services.voice.pipeline import FrontBackground
from services.voice.runtime import Connection
from services.voice.state import Store

OWNER = Identity('Alex', worker='phone', owner=True)


# --- Whisper's gate ------------------------------------------------------------
def seg(text, no_speech=0.05, logprob=-0.2, compression=1.2):
    return {'text': text, 'no_speech_prob': no_speech, 'avg_logprob': logprob, 'compression_ratio': compression}


def test_gate_keeps_real_speech():
    assert stt_gate.judge([seg(' What is the weather tomorrow?')], 1.6) == ('What is the weather tomorrow?', None)


@pytest.mark.parametrize('segments, speech_s, reason', [
    ([], 0.0, 'no_speech'),
    ([seg('Thank you.')], 0.1, 'too_short'),
    ([seg('Thank you.', no_speech=0.4)], 0.9, 'hallucination'),          # stock phrase, Whisper unsure
    ([seg('Thanks for watching!', logprob=-0.6)], 1.2, 'hallucination'),
    ([seg('you')], 0.4, 'hallucination'),                                  # short and stock: needs 0.6 s
    ([seg('Turn it up', no_speech=0.8)], 1.0, 'no_speech'),
    ([seg('Turn it up', logprob=-1.4)], 1.0, 'low_confidence'),
    ([seg('la la la la la la la', compression=3.1)], 2.0, 'repetitive'),
    ([seg('go go go go')], 1.5, 'repetitive'),
    ([seg('  ')], 1.0, 'empty'),
])
def test_gate_drops_noise(segments, speech_s, reason):
    assert stt_gate.judge(segments, speech_s) == ('', reason)


def test_gate_believes_a_clear_thank_you():
    assert stt_gate.judge([seg('Thank you.', no_speech=0.02, logprob=-0.1)], 0.8) == ('Thank you.', None)


def test_gate_stats_have_no_text():
    stats = stt_gate.stats([seg('secret words', no_speech=0.1, logprob=-0.3)], 1.234)
    assert stats == {'speech_s': 1.23, 'no_speech': 0.1, 'logprob': -0.3, 'compression': 1.2}


# --- a connection with a scripted transcriber ----------------------------------
class Provider:
    system, default_voice, supports_activity = 'test', 'v', True

    def __init__(self, transcripts=()):
        self.transcripts = list(transcripts)
        self.chats = 0

    async def transcribe_detail(self, pcm):
        await asyncio.sleep(0)
        text = self.transcripts.pop(0) if self.transcripts else ''
        return {'text': text, 'reason': None if text else 'no_speech', 'stats': {'speech_s': 1.0}}

    async def chat(self, messages, on_clause, **kwargs):
        self.chats += 1
        await on_clause('Classic reply.')
        return 'Classic reply.', []

    async def synthesize(self, text, voice):
        return b'\0' * 2 * 1600, 16000      # 100 ms per clause


class SlowFront:
    """Front stream that speaks the given clauses and blocks until released."""
    def __init__(self, clauses=('Let me check.',), gate=None):
        self.clauses, self.gate, self.requests = clauses, gate, []

    async def __call__(self, messages, on_clause, on_token=None):
        self.requests.append(messages)
        if on_token:
            on_token()
        if self.gate is not None:
            await self.gate.wait()
        for clause in self.clauses:
            await on_clause(clause)
        return ' '.join(self.clauses)


class MCP:
    async def call(self, name, args):
        return json.dumps({'deck': []})


async def no_reads(worker, cap, args):
    return {}


async def no_action(messages, tools, effort):
    return {'tool_calls': [{'id': 'n', 'type': 'function', 'function': {'name': 'no_action', 'arguments': '{}'}}]}


def connection(tmp_path, transcripts=(), front=None, pipeline=True, speakers=None):
    store = Store(tmp_path / 'state.db')
    events = []
    async def send(event):
        events.append(event)
    async def audio(data):
        events.append({'type': '_audio'})
    jobs = Jobs(store, {}, '', 0, lambda *a: None)
    provider = Provider(transcripts)
    conn = Connection(store, jobs, provider, 'session', send, audio, activity=True, enqueue=events.append,
                      identity=OWNER)
    conn.mode = mode('assistant')
    conn.speakers = speakers
    if pipeline:
        conn.pipeline = 'front_background'
        conn.fb = FrontBackground(conn, front=front or SlowFront(), complete=no_action, board=Board(), mcp=MCP(),
                                  read=no_reads)
    return conn, store, jobs, events


async def wait_for(predicate, timeout=3):
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > end:
            raise AssertionError('timed out')
        await asyncio.sleep(.01)


async def shutdown(conn, store, jobs):
    await conn.close()
    await jobs.close()
    store.db.close()


def kinds(events, kind):
    return [e for e in events if e.get('type') == kind]


def test_noise_never_interrupts_a_running_reply(tmp_path, caplog):
    async def scenario():
        gate = asyncio.Event()
        conn, store, jobs, events = connection(tmp_path, transcripts=['tell me a joke', ''], front=SlowFront(gate=gate))
        try:
            with caplog.at_level(logging.INFO, logger='voice.timing'):
                await conn.heard(b'\0' * 32000, time.monotonic())
                await wait_for(lambda: conn.task is not None)
                turn, task, before = conn.epoch, conn.task, len(kinds(events, 'interrupt'))
                await conn.heard(b'\0' * 32000, time.monotonic())     # transcribes to nothing
                assert conn.epoch == turn and not task.done() and len(kinds(events, 'interrupt')) == before
                gate.set()
                await wait_for(lambda: task.done())
        finally:
            await shutdown(conn, store, jobs)
        lines = [json.loads(r.getMessage()) for r in caplog.records if 'voice_turn_timing' in r.getMessage()]
        dropped = [line for line in lines if line['status'] == 'dropped']
        assert dropped and dropped[0]['reason'] == 'no_speech' and 'stt_ms' in dropped[0]
        assert [e['text'] for e in kinds(events, 'stt')] == ['tell me a joke']
    asyncio.run(scenario())


def test_new_words_before_any_reply_merge_into_one_turn(tmp_path):
    async def scenario():
        gate = asyncio.Event()
        front = SlowFront(clauses=('Sure.',), gate=gate)
        conn, store, jobs, events = connection(tmp_path, transcripts=["what's the weather", 'in Austin'], front=front)
        try:
            await conn.heard(b'\0' * 32000, time.monotonic())
            await wait_for(lambda: len(front.requests) == 1)
            first = conn.epoch
            await conn.heard(b'\0' * 32000, time.monotonic())
            await wait_for(lambda: len(front.requests) == 2)
            gate.set()
            await wait_for(lambda: conn.task.done())
            users = [m['content'] for m in store.messages('session') if m['role'] == 'user']
            merged_from = conn.turn_meta[conn.epoch]['merged_from']
        finally:
            await shutdown(conn, store, jobs)
        assert users == ["what's the weather in Austin"]
        assert merged_from == first
        last_user = front.requests[1][-1]
        assert last_user['role'] == 'user' and last_user['content'].endswith("what's the weather in Austin")
        assert "what's the weather\n" not in last_user['content']      # not duplicated
    asyncio.run(scenario())


def test_words_after_the_reply_started_playing_replace_the_turn(tmp_path):
    async def scenario():
        front = SlowFront(clauses=('One.', 'Two.', 'Three.'))
        conn, store, jobs, events = connection(tmp_path, transcripts=['count to three', 'stop'], front=front)
        try:
            await conn.heard(b'\0' * 32000, time.monotonic())
            await wait_for(lambda: conn.audio_sent.get(conn.epoch))
            await conn.heard(b'\0' * 32000, time.monotonic())
            await wait_for(lambda: conn.task.done())
            users = [m['content'] for m in store.messages('session') if m['role'] == 'user']
        finally:
            await shutdown(conn, store, jobs)
        assert users == ['count to three', 'stop'] and kinds(events, 'interrupt')
    asyncio.run(scenario())


def test_history_keeps_only_what_was_heard(tmp_path):
    async def scenario():
        front = SlowFront(clauses=('First part is here.', 'Second part never played.'))
        conn, store, jobs, events = connection(tmp_path, transcripts=['tell me', 'wait'], front=front)
        try:
            await conn.heard(b'\0' * 32000, time.monotonic())
            epoch = conn.epoch
            await wait_for(lambda: len(conn.spoken_audio.get(epoch, [])) == 2)
            # The phone acknowledged playback up to half of the second clause.
            first, second = conn.spoken_audio[epoch][0][2], conn.spoken_audio[epoch][1][2]
            conn.audio_played[epoch] = first + (second - first) // 2
            await conn.heard(b'\0' * 32000, time.monotonic())
            await wait_for(lambda: conn.task.done() and conn.epoch > epoch)
            replies = [m['content'] for m in store.messages('session') if m['role'] == 'assistant']
        finally:
            await shutdown(conn, store, jobs)
        assert replies[0] == 'First part is here. Second part [cut off]'
        assert conn.fb.transcript.messages[1] == {'role': 'assistant', 'content': replies[0]} or \
            {'role': 'assistant', 'content': replies[0]} in conn.fb.transcript.messages
    asyncio.run(scenario())


def test_heard_text_without_acknowledgements_uses_the_clock(tmp_path):
    conn, store, jobs, events = connection(tmp_path, pipeline=False)
    conn.spoken_audio[3] = [('Hello there.', 0, 24000), ('Bye now.', 24000, 48000)]
    conn.audio_rate[3] = 24000
    conn.audio_sent[3] = 48000
    conn.play_until = time.monotonic() + 1.0          # one clause still queued at the phone
    assert conn.heard_text(3, ['Hello there.', 'Bye now.']) == 'Hello there. [cut off]'
    assert conn.heard_text(3, ['Hello there.', 'Never said.', 'Bye now.']) == 'Hello there. [cut off]'
    conn.play_until = 0
    assert conn.heard_text(3, ['Hello there.', 'Bye now.']) == 'Hello there. Bye now.'
    store.db.close()


# --- confirmed barge-in -----------------------------------------------------------
def test_hold_pauses_audio_and_noise_resumes_it(tmp_path):
    async def scenario():
        front = SlowFront(clauses=('A fairly long sentence to speak.',) * 3)
        conn, store, jobs, events = connection(tmp_path, transcripts=['go on', ''], front=front)
        conn.barge_confirm = True
        try:
            await conn.heard(b'\0' * 32000, time.monotonic())
            await wait_for(lambda: conn.audio_sent.get(conn.epoch))
            conn.hold()
            sent, interrupts = len(kinds(events, '_audio')), len(kinds(events, 'interrupt'))
            await asyncio.sleep(.15)
            assert len(kinds(events, '_audio')) <= sent + 1     # at most the packet in flight
            await conn.heard(b'\0' * 32000, time.monotonic(), held=True)   # it was nothing
            assert conn.held_since is None
            assert kinds(events, 'resume') and len(kinds(events, 'interrupt')) == interrupts
            await wait_for(lambda: conn.task.done())
            assert len(kinds(events, '_audio')) > sent + 1
        finally:
            await shutdown(conn, store, jobs)
    asyncio.run(scenario())


def test_hold_then_real_words_interrupt(tmp_path):
    async def scenario():
        front = SlowFront(clauses=('A fairly long sentence to speak.',) * 3)
        conn, store, jobs, events = connection(tmp_path, transcripts=['go on', 'no stop'], front=front)
        conn.barge_confirm = True
        try:
            await conn.heard(b'\0' * 32000, time.monotonic())
            await wait_for(lambda: conn.audio_sent.get(conn.epoch))
            first, interrupts = conn.epoch, len(kinds(events, 'interrupt'))
            conn.hold()
            await conn.heard(b'\0' * 32000, time.monotonic(), held=True)
            assert conn.held_since is None and conn.epoch > first
            assert len(kinds(events, 'interrupt')) > interrupts and not kinds(events, 'resume')
        finally:
            await shutdown(conn, store, jobs)
    asyncio.run(scenario())


def test_hold_with_no_utterance_resumes_after_quiet(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(runtime_mod, 'HOLD_QUIET_S', 0.05)
        conn, store, jobs, events = connection(tmp_path, pipeline=False)
        conn.play_until = until = time.monotonic() + 5
        conn.hold()
        assert conn.check_hold(utterance_open=True) is None
        await asyncio.sleep(.08)
        resumed = conn.check_hold(utterance_open=False)
        assert resumed is not None
        await resumed
        assert conn.held_since is None and kinds(events, 'resume')
        assert conn.play_until >= until + .08       # playback stood still while held
        store.db.close()
    asyncio.run(scenario())


# --- echo guard ------------------------------------------------------------------
class Speakers:
    available = True

    def __init__(self, label):
        self.label = label

    async def identify(self, pcm, assistant_voice=None):
        return {'label': self.label, 'score': 0.2, 'assistant_score': 0.9 if self.label == 'assistant' else 0.1,
                'principal': None, 'ms': 5}


def test_echo_guard_drops_the_assistants_own_voice_while_it_speaks(tmp_path, monkeypatch, caplog):
    async def scenario():
        monkeypatch.setattr(runtime_mod, 'ECHO_GUARD', 'enforce')
        conn, store, jobs, events = connection(tmp_path, transcripts=['the capital of france is paris'],
                                               pipeline=False, speakers=Speakers('assistant'))
        conn.play_until = time.monotonic() + 2
        with caplog.at_level(logging.INFO, logger='voice.timing'):
            await conn.heard(b'\0' * 32000, time.monotonic())
        assert conn.task is None and not kinds(events, 'stt')
        line = json.loads([r.getMessage() for r in caplog.records if 'dropped' in r.getMessage()][0])
        assert line['reason'] == 'echo' and line['speaker'] == 'assistant'
        store.db.close()
    asyncio.run(scenario())


def test_speaker_label_is_logged_in_shadow_mode(tmp_path, monkeypatch, caplog):
    async def scenario():
        monkeypatch.setattr(runtime_mod, 'ECHO_GUARD', 'shadow')
        conn, store, jobs, events = connection(tmp_path, transcripts=['hello there'],
                                               speakers=Speakers('assistant'))
        conn.play_until = time.monotonic() + 2
        try:
            with caplog.at_level(logging.INFO, logger='voice.timing'):
                await conn.heard(b'\0' * 32000, time.monotonic())
                await wait_for(lambda: conn.task is not None and conn.task.done())
                await wait_for(lambda: not conn.fb.backgrounds)
        finally:
            await shutdown(conn, store, jobs)
        line = json.loads([r.getMessage() for r in caplog.records if 'voice_turn_timing' in r.getMessage()][-1])
        assert line['status'] == 'ok' and line['speaker'] == 'assistant' and line['assistant_score'] == 0.9
    asyncio.run(scenario())


class SequenceSpeakers:
    """Speaker ID that hears the given (label, owner score) per utterance, in order;
    only the owner ``Alex`` is enrolled, and every turn has a voiceprint."""
    available, threshold = True, 0.45

    def __init__(self, *heard):
        self.heard, self.learned, self.cohort = list(heard), [], []

    async def identify(self, pcm, assistant_voice=None):
        label, score = self.heard.pop(0)
        return {'label': label, 'score': score, 'principal': 'Alex' if label == 'Alex' else None,
                'candidate': 'Alex', 'scores': {'Alex': score}, 'assistant_score': 0.0, 'ms': 5,
                'norm': None, 'clip': {'id': 1, 'pinned': True, 'device': 'enrollment', 'setting': 'enrollment'},
                'vector': [score]}

    def has_assistant_voice(self, voice):
        return True

    def learn_sample(self, principal, vector, pcm=None, device='', session=''):
        self.learned.append((principal, vector[0], device))
        return {'ok': True, 'version': 100 + len(self.learned)}

    def add_cohort(self, vector, source='stranger'):
        self.cohort.append(vector[0])
        return True



class TimedProvider(Provider):
    """Speech length from the audio (16 kHz PCM16), as the real STT stats give it."""
    async def transcribe_detail(self, pcm):
        reply = await super().transcribe_detail(pcm)
        reply['stats'] = {'speech_s': round(len(pcm) / 32000, 2)}
        return reply


def test_speaker_does_not_flip_mid_turn_on_a_weak_match(tmp_path, caplog):
    """Bake 2026-10-27: mid-conversation, mid-turn, the voice said "I can only do that for
    Bake" to Bake. On the owner's phone a weak match (his own short or noisy line, 0.25)
    stays the owner; only a strong mismatch on real speech is someone else."""
    def lines():
        return [line for line in (json.loads(r.getMessage()) for r in caplog.records
                                  if 'voice_turn_timing' in r.getMessage()) if 'speaker_basis' in line]

    async def scenario():
        gate = asyncio.Event()
        speakers = SequenceSpeakers(('Alex', 0.56), ('guest', 0.25), ('guest', 0.05), ('guest', 0.05))
        conn, store, jobs, events = connection(tmp_path, front=SlowFront(gate=gate), speakers=speakers,
                                               transcripts=['what is on my calendar', 'and tomorrow',
                                                            'yes', 'can I play a game'])
        conn.provider = TimedProvider(conn.provider.transcripts)
        try:
            with caplog.at_level(logging.INFO, logger='voice.timing'):
                await conn.heard(b'\0' * 64000, time.monotonic())            # 2 s, a sure match
                await wait_for(lambda: conn.task is not None)
                await conn.heard(b'\0' * 51200, time.monotonic())            # 1.6 s, mid-turn, weak
                gate.set()
                await wait_for(lambda: conn.task is not None and conn.task.done())
                for n, size in ((3, 22400), (4, 64000)):  # 0.7 s, too short to judge; 2 s, clearly not him
                    await conn.heard(b'\0' * size, time.monotonic())
                    await wait_for(lambda: len(lines()) >= n)
        finally:
            await shutdown(conn, store, jobs)
        return [(line['speaker'], line['speaker_sure'], line['speaker_basis']) for line in lines()]
    heard = asyncio.run(scenario())
    assert heard[0] == ('Alex', True, 'voice')
    assert ('Alex', True, 'owner phone') in heard                        # the weak and the short line
    assert heard.count(('guest', False, 'voice')) == 1                   # only the strong mismatch


def run_owner_turns(tmp_path, *heard, device='phone', debug=False, identity=None):
    """Owner-phone utterances of 1.5 s each, one after another; returns (speakers, events)."""
    async def scenario():
        speakers = SequenceSpeakers(*heard)
        conn, store, jobs, events = connection(tmp_path, speakers=speakers, pipeline=False,
                                               transcripts=['hello'] * len(heard))
        conn.provider = TimedProvider(conn.provider.transcripts)
        conn.identity = identity or Identity('Alex', worker=device, owner=True)
        conn.speaker_debug = debug
        try:
            for _ in heard:
                await conn.heard(b'\0' * 48000, time.monotonic())
                await wait_for(lambda: conn.task is not None and conn.task.done())
        finally:
            await shutdown(conn, store, jobs)
        return speakers, events
    return asyncio.run(scenario())


def test_owner_voice_is_learned_only_in_an_anchored_connection(tmp_path):
    """After a confident owner match on the owner's device, the anchor persists.
    Only strong matches (>= threshold) are learned or update the anchor. Weak turns
    are not learned even with an active anchor. Nothing is learned when identified as someone else."""
    learned = lambda *heard: [score for _, score, _ in run_owner_turns(tmp_path, *heard)[0].learned]
    assert learned(('guest', 0.35), ('guest', 0.40)) == []
    assert learned(('Alex', 0.6), ('guest', 0.35), ('guest', 0.2), ('guest', 0.40), ('Alex', 0.5)) == []
    speakers, _ = run_owner_turns(tmp_path, ('Alex', 0.6), ('Alex', 0.44), device='bakewatch')
    assert speakers.learned == []


def test_speaker_debug_event_per_utterance(tmp_path):
    speakers, events = run_owner_turns(tmp_path, ('Alex', 0.6), ('guest', 0.35), ('guest', 0.05), debug=True)
    lines = [e for e in events if e.get('type') == 'speaker']
    assert [(e['label'], e['heard'], e['score'], e['threshold'], e['basis'], e['learned']) for e in lines] == [
        ('Alex', 'Alex', 0.6, 0.45, 'voice', None),
        ('Alex', 'guest', 0.35, 0.45, 'owner phone', None),
        ('guest', 'guest', 0.05, 0.45, 'voice', None)]
    assert 'vector' not in lines[0] and json.dumps(lines)                # no embedding goes to the app
    assert not [e for e in run_owner_turns(tmp_path, ('Alex', 0.6))[1] if e.get('type') == 'speaker']
    guest = Identity('Kid', worker='tablet')
    assert not [e for e in run_owner_turns(tmp_path, ('guest', 0.6), debug=True, identity=guest)[1]
                if e.get('type') == 'speaker']


# --- Front transcript ----------------------------------------------------------------
def test_transcript_requests_extend_each_other():
    t = front_mod.Transcript(max_chars=10_000)
    past = [{'role': 'user', 'content': 'earlier'}, {'role': 'assistant', 'content': 'ok'}]
    facts = [{'key': 'time', 'text': 'It is noon.'}]
    a = t.request('FIXED', past, facts, 'hi', preface='[About the user: likes tea]')
    t.commit('Hello!')
    b = t.request('FIXED', past + [{'role': 'user', 'content': 'hi'}], facts, 'and you?')
    assert b[:len(a)] == a and b[len(a)] == {'role': 'assistant', 'content': 'Hello!'}
    assert 'likes tea' in a[-1]['content'] and 'It is noon' in a[-1]['content']
    assert b[-1] == {'role': 'user', 'content': 'and you?'}             # the fact was told already
    t.commit('Fine.')
    c = t.request('FIXED', past, [{'key': 'time', 'text': 'It is 12:01.'}], 'time?')
    assert c[-1]['content'] == '[Known facts now (current; you may state these): It is 12:01.]\ntime?'


def test_transcript_restarts_when_fixed_changes_or_it_grows():
    t = front_mod.Transcript(max_chars=200, keep_turns=6)
    t.request('A', [], [], 'x' * 150)
    t.commit('Yes. ' * 20)
    assert t.resets == 1
    t.request('A', [{'role': 'user', 'content': 'q'}, {'role': 'assistant', 'content': 'Right.'}], [], 'next')
    assert t.resets == 2 and t.messages == [{'role': 'user', 'content': 'q'}, {'role': 'assistant', 'content': 'Right.'}]
    t.commit('Okay.')
    t.request('B', [], [], 'again')
    assert t.resets == 3


def test_unanswered_line_joins_the_next():
    t = front_mod.Transcript()
    t.request('F', [], [], 'first')
    t.commit('')
    m = t.request('F', [], [], 'second')
    assert m[1:] == [{'role': 'user', 'content': 'first\nsecond'}]


def test_early_first_clause_at_a_comma():
    assert front_mod.early_clause('Well, ') == ([], 'Well, ')
    assert front_mod.early_clause('The capital of France is Paris, which ') == \
        (['The capital of France is Paris,'], 'which ')


# --- warm start and the house agent -------------------------------------------------
class HouseMCP:
    def __init__(self, answer='Done: I added milk to the shopping list.'):
        self.calls, self.answer = [], answer

    async def call(self, name, args):
        self.calls.append((name, args))
        if args.get('cap') == 'memory.list':
            return json.dumps({'ok': True, 'result': {'items': [{'text': 'Bake prefers short answers.'}]}})
        if args.get('cap') == 'home.ask':
            return json.dumps({'ok': True, 'result': {'answer': self.answer, 'model': 'test'}})
        return json.dumps({'deck': []})


def test_warm_start_then_first_turn_extends_it(tmp_path):
    async def scenario():
        front = SlowFront(clauses=('Ready.',))
        conn, store, jobs, events = connection(tmp_path, transcripts=['hello'], front=front)
        conn.fb = FrontBackground(conn, front=front, complete=no_action, board=Board(), mcp=HouseMCP(), read=no_reads)
        try:
            conn.fb.load_house_context()
            await conn.fb.warm()
            assert not kinds(events, '_audio')              # the warm-up is never spoken
            await conn.heard(b'\0' * 32000, time.monotonic())
            await wait_for(lambda: conn.task is not None and conn.task.done())
        finally:
            await shutdown(conn, store, jobs)
        warm, first = front.requests[0], front.requests[1]
        assert 'Bake prefers short answers' in warm[-1]['content'] and 'has not spoken yet' in warm[-1]['content']
        # The warm-up stays in the transcript: it must not tell the model how to reply,
        # or later turns obey it too ("I am here." to every line, 2026-10-27).
        assert not re.search(r'(?i)\b(reply|say|answer)\b', FrontBackground.WARM_NOTE)
        assert first[:len(warm)] == warm and first[len(warm)] == {'role': 'assistant', 'content': 'Ready.'}
        assert first[-1]['content'].endswith('hello')
    asyncio.run(scenario())


def test_house_agent_tool_asks_the_hub_and_is_owner_only():
    from services.voice.bgtools import Toolbox
    from services.voice.policy import background_tools, BASE_TOOLS
    mcp = HouseMCP()
    box = Toolbox('s', None, Board(), mcp=mcp)
    answer = asyncio.run(box.run('house_agent', {'request': 'Add milk to the shopping list.'}))
    assert answer == 'Done: I added milk to the shopping list.'
    name, args = mcp.calls[0]
    assert name == 'rook_call' and args['worker'] == 'rook' and args['cap'] == 'home.ask'
    assert args['args']['question'] == 'Add milk to the shopping list.' and 'spoken' in args['args']['context']
    assert 'house_agent' in background_tools(OWNER, 'assistant')
    assert 'house_agent' not in background_tools(Identity('Kid', worker='kidphone'), 'assistant')
    assert 'house_agent' not in background_tools(OWNER, 'conversation') and 'house_agent' not in BASE_TOOLS


# --- echo by text, enrollment commands -------------------------------------------------
def test_echo_text_matches_what_was_just_said(tmp_path):
    conn, store, jobs, events = connection(tmp_path, pipeline=False)
    conn.epoch = 4
    conn.spoken_audio[4] = [('Once upon a time there was a very sleepy cat.', 0, 100)]
    assert conn.echo_text('there was a very sleepy cat')
    assert conn.echo_text('Once upon a time, there was')
    assert not conn.echo_text('stop, what is two plus two')
    assert not conn.echo_text('sleepy cat')                # two words are never enough
    store.db.close()


def test_echoed_words_never_interrupt(tmp_path, caplog):
    async def scenario():
        front = SlowFront(clauses=('The weather today is sunny and warm.',) * 4)
        conn, store, jobs, events = connection(tmp_path, transcripts=['weather', 'today is sunny and warm'],
                                               front=front)
        try:
            await conn.heard(b'\0' * 32000, time.monotonic())
            await wait_for(lambda: conn.audio_sent.get(conn.epoch))
            epoch = conn.epoch
            with caplog.at_level(logging.INFO, logger='voice.timing'):
                await conn.heard(b'\0' * 32000, time.monotonic())
            assert conn.epoch == epoch
        finally:
            await shutdown(conn, store, jobs)
        assert any('"reason":"echo_text"' in r.getMessage() for r in caplog.records)
    asyncio.run(scenario())


class EnrollSpeakers:
    available = True

    def __init__(self):
        self.samples, self.prints = {}, {}

    async def identify(self, pcm, assistant_voice=None):
        return {'label': 'guest', 'score': 0.1, 'assistant_score': None}

    def has_assistant_voice(self, voice):
        return True

    def enrollment_sentences(self):
        return ['One fish.', 'Two fish.', 'Red fish.', 'Blue fish.']

    async def enroll_add(self, principal, pcm):
        n = self.samples[principal] = self.samples.get(principal, 0) + 1
        return {'ok': True, 'samples': n, 'needed': 3}

    def enroll_finish(self, principal):
        self.prints[principal] = True
        return True

    def forget(self, principal):
        return self.prints.pop(principal, None) is not None


def test_enroll_my_voice_reads_three_sentences_then_forget(tmp_path):
    async def scenario():
        speakers = EnrollSpeakers()
        conn, store, jobs, events = connection(
            tmp_path, transcripts=['Enroll my voice please', 'one fish', 'two fish', 'red fish', 'forget my voice'],
            pipeline=False, speakers=speakers)
        try:
            for _ in range(5):
                await conn.heard(b'\0' * 32000, time.monotonic())
                await wait_for(lambda: conn.task is not None and conn.task.done())
        finally:
            await shutdown(conn, store, jobs)
        said = [e['text'] for e in kinds(events, 'assistant_delta')]
        assert 'Please read: One fish.' in said and 'Next, please read: Two fish.' in said
        assert any("I've learned your voice" in s for s in said) and 'Done, I deleted your voiceprint.' in said
        assert speakers.samples == {'Alex': 3} and speakers.prints == {}
        assert conn.provider.chats == 0                    # never a model turn
    asyncio.run(scenario())


def test_voice_commands_only_at_the_start():
    assert runtime_mod.ENROLL_RE.search('Hey Sojourn, please enroll my voice')
    assert runtime_mod.ENROLL_RE.search('learn my voice')
    assert not runtime_mod.ENROLL_RE.search('you know my voice is hoarse today')
    assert not runtime_mod.ENROLL_RE.search('I said remember my voice memo')
    assert runtime_mod.FORGET_RE.search('Okay Rook, forget my voice.')
    assert not runtime_mod.FORGET_RE.search("don't forget my voice lesson at five")


def test_only_owner_keys_enroll(tmp_path):
    async def scenario():
        conn, store, jobs, events = connection(tmp_path, transcripts=['learn my voice'], pipeline=False,
                                               speakers=EnrollSpeakers())
        conn.identity = Identity('Kid', worker='kidphone')
        try:
            await conn.heard(b'\0' * 32000, time.monotonic())
            await wait_for(lambda: conn.task is not None and conn.task.done())
        finally:
            await shutdown(conn, store, jobs)
        assert conn.enrolling is None
        assert any('needs the owner' in e['text'] for e in kinds(events, 'assistant_delta'))
    asyncio.run(scenario())


def test_reply_waits_for_the_user_to_finish_then_merges(tmp_path):
    async def scenario():
        conn, store, jobs, events = connection(tmp_path, transcripts=['what is the tallest mountain', 'in Africa'],
                                               front=SlowFront(clauses=('Mount Kilimanjaro.',)))
        try:
            conn.receiving_speech = True                       # the user is still talking
            await conn.heard(b'\0' * 32000, time.monotonic())
            await asyncio.sleep(.2)
            assert not kinds(events, '_audio')                 # no talking over the user
            conn.receiving_speech = False
            await conn.heard(b'\0' * 32000, time.monotonic())
            await wait_for(lambda: conn.task.done() and kinds(events, '_audio'))
            users = [m['content'] for m in store.messages('session') if m['role'] == 'user']
        finally:
            await shutdown(conn, store, jobs)
        assert users == ['what is the tallest mountain in Africa']
    asyncio.run(scenario())


def test_house_agent_answer_ends_the_background_run():
    from services.voice.bgtools import Toolbox
    from services.voice.identity import current_identity
    from services.voice.pipeline import BackgroundAgent
    from services.voice.policy import background_tools
    calls = []
    async def complete(messages, tools, effort):
        calls.append(1)
        return {'tool_calls': [{'id': 'h', 'type': 'function', 'function': {
            'name': 'house_agent', 'arguments': json.dumps({'request': 'What is on the shopping list?'})}}]}
    async def scenario():
        current_identity.set(OWNER)
        agent = BackgroundAgent(Toolbox('s', None, Board(), mcp=HouseMCP('Milk and eggs.')),
                                background_tools(OWNER, 'assistant'), complete=complete)
        return await agent.run('what is on the shopping list', [], lambda u: None)
    assert asyncio.run(scenario()) == 'Milk and eggs.' and len(calls) == 1
