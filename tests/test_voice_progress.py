import asyncio
import time

import pytest

from services.voice.progress import ProgressUpdates
from test_voice_activity import connection, Provider


@pytest.mark.parametrize('config,first,every,enabled', [
    (None, 25, 45, True), ({'enabled': False}, 25, 45, False),
    ({'first_after_s': 2, 'every_s': 4}, 2, 4, True),
    ({'first_after_s': float('nan'), 'every_s': -1}, 25, 45, True),
])
def test_config(config, first, every, enabled):
    p = ProgressUpdates(None, config)
    assert (p.first, p.every, p.enabled) == (first, every, enabled)


@pytest.mark.parametrize('suppression', ['speech', 'recent', 'playback', 'sleep'])
def test_slow_job_timing_suppression_completion_and_no_llm(tmp_path, monkeypatch, suppression):
    monkeypatch.setattr(ProgressUpdates, 'poll_seconds', .005)
    monkeypatch.setattr(ProgressUpdates, 'quiet_seconds', .03)
    async def scenario():
        class NoPlanner(Provider):
            async def chat(self, *a, **k): pytest.fail('Progress called the LLM')
        conn, events, close = await connection(tmp_path, provider=NoPlanner())
        conn.drain_results = lambda: None
        p = conn.progress
        p.first, p.every = .025, .02
        job_done = asyncio.Event()
        async def slow(args):
            await job_done.wait()
            return 'done'
        conn.jobs.direct['rook_read'] = slow
        jid = conn.jobs.start('session', 'rook_read', {'worker': 'kaiju'}, [])
        p.start(jid, 0, 'rook_read', {'worker': 'kaiju'})
        if suppression == 'speech': conn.receiving_speech = True
        if suppression == 'recent': conn.last_speech = time.monotonic() + .04
        if suppression == 'playback': conn.play_until = time.monotonic() + 1
        if suppression == 'sleep': conn.sleeping = True
        try:
            await asyncio.sleep(.05)
            assert not any(e.get('phase') == 'progress' for e in events)
            conn.receiving_speech = False
            conn.last_speech = 0
            conn.play_until = 0
            conn.sleeping = False
            await asyncio.sleep(.02)
            progress = [e for e in events if e.get('phase') == 'progress']
            assert len(progress) == 1
            assert progress[0]['label'] == 'Still waiting on kaiju.'
            assert any(e['type'] == 'audio_sr' for e in events)
            job_done.set()
            await asyncio.gather(*list(conn.jobs.tasks.values()))
            await asyncio.sleep(.08)
            assert len([e for e in events if e.get('phase') == 'progress']) == 1
            assert not p.jobs
        finally: await close()
    asyncio.run(scenario())


def test_max_three_then_final_and_real_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(ProgressUpdates, 'poll_seconds', .002)
    async def scenario():
        conn, events, close = await connection(tmp_path)
        p = conn.progress
        p.first = p.every = .006
        lines = []
        async def say(text, job): lines.append(text)
        conn.say_progress = say
        p.start('job', 0, 'rook_read', {'worker': 'kaiju'})
        try:
            await asyncio.sleep(.004)
            assert lines == []
            await asyncio.sleep(.06)
            assert len(lines) == 4
            assert lines[0] == 'Still waiting on kaiju.'
            assert 'rook read on kaiju' in lines[1]
            assert lines[-1] == "This is taking a while - I'll tell you when it's done."
            await asyncio.sleep(.03)
            assert len(lines) == 4
            p.event({'id': 'job', 'status': 'running', 'progress': 'Downloaded 4 of 8 files'})
            assert ProgressUpdates.line({**p.jobs['job'], 'count': 0}) == 'rook read: Downloaded 4 of 8 files'
        finally: await close()
    asyncio.run(scenario())


def test_cancel_during_synthesis_prevents_progress_audio(tmp_path, monkeypatch):
    monkeypatch.setattr(ProgressUpdates, 'poll_seconds', .002)
    async def scenario():
        synthesizing = asyncio.Event()
        class SlowTTS(Provider):
            async def synthesize(self, *args):
                synthesizing.set()
                await asyncio.Event().wait()
        conn, events, close = await connection(tmp_path, provider=SlowTTS())
        conn.progress.first = .002
        conn.progress.start('job', 0, 'rook_read', {'worker': 'kaiju'})
        try:
            await asyncio.wait_for(synthesizing.wait(), .5)
            conn.job_event({'id': 'job', 'status': 'cancel_requested'})
            await asyncio.sleep(.01)
            assert not any(e.get('phase') == 'progress' or e['type'] == 'audio_sr' for e in events)
        finally: await close()
    asyncio.run(scenario())


def test_quick_job_finishes_without_progress(tmp_path, monkeypatch):
    monkeypatch.setattr(ProgressUpdates, 'poll_seconds', .002)
    async def scenario():
        conn, events, close = await connection(tmp_path)
        conn.progress.first = .03
        conn.progress.start('quick', 0, 'rook_read', {'worker': 'kaiju'})
        try:
            await asyncio.sleep(.005)
            conn.job_event({'id': 'quick', 'status': 'completed'})
            await asyncio.sleep(.04)
            assert not any(e.get('phase') == 'progress' for e in events)
        finally: await close()
    asyncio.run(scenario())
