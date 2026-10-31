"""A throwaway Rook 0.5 hub for WatchSpeechLiveTest (the watch's 0.5 voice, app unit tests).

    python android/live_speech_hub.py ./gradlew :app:testDebugUnitTest --tests '*WatchSpeechLiveTest*'

Runs a hub in this process (HTTP door on 127.0.0.1, a temp folder) with the real speech and
agents plugins and the fakes from tests/speech_fakes.py, except that the fake engine speaks
real 24 kHz WAV. Then it runs the command (from android/) with ROOK_LIVE_DOOR and
ROOK_LIVE_TOKEN (the owner's) set, and stops the hub. Needs the Rook test venv (rook-pytest's).
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from rook.core2.hub.runtime import Config, Hub, Loopback  # noqa: E402
from speech_fakes import BRAIN, BRAIN_PY, ENGINE, ENGINE_PY, TALKER, TALKER_PY, plugin  # noqa: E402

# The fakes' engine, but tts answers a real WAV (the app decodes it): 10 ms of silence a character.
WAV_TTS = '''

def tts(ctx, text, voice=None):
    import io, wave
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
        w.writeframes(bytes(480 * len(text)))
    return ctx.file(data=out.getvalue(), name="speech.wav", mime="audio/wav")
'''


async def main(cmd: list[str]) -> int:
    with tempfile.TemporaryDirectory(prefix="rook-live-speech-") as tmp:
        lib = Path(tmp) / "lib"
        plugin(lib, "speech-engine", ENGINE, "engine.py", ENGINE_PY + WAV_TTS)
        plugin(lib, "agents-local", BRAIN, "brain.py", BRAIN_PY)
        plugin(lib, "llm-openai", TALKER, "talker.py", TALKER_PY)
        for name in ("speech", "agents"):
            (lib / name).symlink_to(ROOT / "rook" / "plugins" / name, target_is_directory=True)
        cfg = Config(data=Path(tmp) / "data", band="e17904e6ed4c664592f64f6f1d02c682", library=lib,
                     users={"bake": {"preset": "owner", "created": time.time()}},
                     settings={"agents": {"warm": "5s"}}, doors={"http": {"bind": "127.0.0.1:0"}})
        hub = Hub(cfg, transport=Loopback())
        await hub.start()
        try:
            env = {**os.environ, "ROOK_LIVE_DOOR": f"http://127.0.0.1:{hub.doors[0].port}",
                   "ROOK_LIVE_TOKEN": (hub.data / "owner.token").read_text().strip()}
            proc = await asyncio.create_subprocess_exec(*cmd, cwd=ROOT / "android", env=env)
            return await asyncio.wait_for(proc.wait(), 1800)
        finally:
            await hub.stop()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    sys.exit(asyncio.run(main(sys.argv[1:])))
