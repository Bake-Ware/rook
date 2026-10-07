"""``POST /api/transcribe``: speech to text with the service's loaded Whisper.

Used by the phone's reply window after ``voice.speak(reply=true)`` (see
docs/design/voice-replies.md). Body: raw 16 kHz mono signed 16-bit little-endian
PCM (``Content-Type: audio/L16``), at most 30 s. Answers ``{text, seconds}``;
``text`` is empty when nothing intelligible was said. ``authorize(bearer)``
applies the same key/guest rule as ``/api/voice``.
"""
import asyncio

from fastapi import HTTPException, Request

SAMPLE_RATE = 16000
MAX_SECONDS = 30
MAX_BYTES = MAX_SECONDS * SAMPLE_RATE * 2


def install(app, authorize):
    slot = asyncio.Semaphore(2)

    @app.post('/api/transcribe')
    async def transcribe(request: Request):
        supplied = request.headers.get('authorization', '').removeprefix('Bearer ')
        if not authorize(supplied):
            raise HTTPException(401, 'A valid voice key is required.')
        kind = request.headers.get('content-type', '').split(';')[0].strip().lower()
        if kind not in ('audio/l16', 'application/octet-stream'):
            raise HTTPException(415, 'Send 16 kHz mono 16-bit PCM as audio/L16.')
        try:
            length = int(request.headers.get('content-length', '0'))
        except ValueError:
            raise HTTPException(411, 'Content-Length is required.')
        if not 0 < length <= MAX_BYTES:
            raise HTTPException(413, f'Audio must be 1 byte to {MAX_SECONDS} s.')
        try:
            pcm = await asyncio.wait_for(request.body(), 20)
        except asyncio.TimeoutError:
            raise HTTPException(408, 'Upload too slow.')
        if len(pcm) > MAX_BYTES or len(pcm) % 2:
            raise HTTPException(400, 'Audio must be whole 16-bit samples, at most 30 s.')
        if slot.locked():
            raise HTTPException(503, 'Transcription is busy.')
        async with slot:
            try:
                text = await asyncio.wait_for(request.app.state.provider.transcribe(pcm), 30)
            except Exception as error:
                raise HTTPException(503, 'Transcription failed or timed out.') from error
        return {'text': (text or '').strip(), 'seconds': round(len(pcm) / (2 * SAMPLE_RATE), 2)}
