"""Install raw speech on an existing FastAPI voice service and loaded provider.

Use install(app, authorize), where authorize(bearer_token) checks the service's
existing key store. The provider supplies voices, default_voice and async
synthesize(text, voice) returning mono signed 16-bit PCM and a sample rate.
"""
import asyncio
import io
import json
import wave
from fastapi import HTTPException, Request, Response


def install(app, authorize):
    slot = asyncio.Semaphore(1)

    @app.post('/api/voice')
    async def voice(request: Request):
        supplied = request.headers.get('authorization', '').removeprefix('Bearer ')
        if not supplied or not authorize(supplied):
            raise HTTPException(401, 'A valid voice key is required.')
        if request.headers.get('content-type', '').split(';')[0].strip() != 'application/json':
            raise HTTPException(415, 'Send JSON.')
        try:
            length = int(request.headers.get('content-length', '0'))
            if not 0 < length <= 4096:
                raise ValueError('Body must contain at most 4096 bytes.')
            body = await asyncio.wait_for(request.body(), 5)
            if len(body) > 4096:
                raise ValueError('Body is too large.')
            message = json.loads(body)
            if not isinstance(message, dict) or set(message) - {'text', 'voice'}:
                raise ValueError('Supply text and an optional voice.')
            text = message.get('text')
            if not isinstance(text, str) or not text.strip() or len(text) > 700:
                raise ValueError('Text must contain 1–700 characters.')
            provider = request.app.state.provider
            selected_voice = message.get('voice') or provider.default_voice
            if selected_voice not in provider.voices:
                raise ValueError('Unknown voice. Inspect /voices.')
        except (ValueError, TypeError, asyncio.TimeoutError) as error:
            raise HTTPException(400, str(error)) from error
        if slot.locked():
            raise HTTPException(503, 'Voice engine is busy.')
        async with slot:
            try:
                pcm, rate = await asyncio.wait_for(provider.synthesize(' '.join(text.split()), selected_voice), 25)
                if not 8000 <= int(rate) <= 192000 or not pcm or len(pcm) > 8 * 1024 * 1024:
                    raise ValueError('Invalid synthesized audio.')
                audio = io.BytesIO()
                with wave.open(audio, 'wb') as file:
                    file.setnchannels(1)
                    file.setsampwidth(2)
                    file.setframerate(int(rate))
                    file.writeframes(pcm)
                return Response(audio.getvalue(), media_type='audio/wav', headers={'Cache-Control': 'no-store'})
            except Exception as error:
                raise HTTPException(503, 'Speech synthesis failed or timed out.') from error
