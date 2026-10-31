"""The voice service's 0.5 worker (services/voice/band.py) with fake engines,
for test_speech_e2e: "audio" is the spoken text as L16 bytes."""
import sys

from services.voice import band


class Engine:
    async def stt(self, pcm, language=None):
        return {"text": pcm.decode().strip()}

    async def tts(self, text, voice):
        return text.encode().ljust(len(text) // 2 * 2 + 2, b" "), 24000

    async def turn(self, pcm):
        return not pcm.decode().strip().endswith("...")

    async def speaker(self, pcm):
        return {"label": "bake", "score": 0.9, "vector": [0.1]}

    def voices(self):
        return {"voices": ["af_heart"], "default": "af_heart"}

    def status(self):
        return {"stt": True, "fake": True}


if __name__ == "__main__":
    sys.exit(band.main(sys.argv[1:], engine=Engine()))
