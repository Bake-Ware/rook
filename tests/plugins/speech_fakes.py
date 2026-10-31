"""Fakes for the speech tests: a speech engine as a hub plugin (the same cap
names and contracts as the real builtin one on kaiju), System One as an
llm.complete provider, and an agent backend. "Audio" in these tests is the
spoken text itself, base64 in an audio/L16 file map."""

import base64

ENGINE = """\
manifest: 1
plugin: speech-engine
version: 1.0.0
doc: A fake speech engine.
runs_on: hub
caps:
  stt:
    doc: Fake speech to text.
    on: hub
    risk: read
    scope: local
    run: scripts/engine.py:stt
    args:
      audio: { type: file, required: true }
      language: { type: str }
    result: { type: map, fields: { text: { type: str, required: true }, language: { type: str } } }
  tts:
    doc: Fake text to speech.
    on: hub
    risk: read
    scope: local
    run: scripts/engine.py:tts
    args:
      text: { type: str, required: true }
      voice: { type: str }
    result: { type: file }
  turn:
    doc: Fake end of turn.
    on: hub
    risk: read
    scope: local
    run: scripts/engine.py:turn
    args:
      audio: { type: file, required: true }
    result: { type: map, fields: { complete: { type: bool, required: true } } }
  speaker:
    doc: Fake speaker ID.
    on: hub
    risk: read
    scope: local
    run: scripts/engine.py:speaker
    args:
      audio: { type: file, required: true }
    result: { type: map, fields: { label: { type: str, required: true }, score: { type: float } } }
  voices:
    doc: Fake voices.
    on: hub
    risk: read
    scope: local
    run: scripts/engine.py:voices
    result: { type: map }
  status:
    doc: Fake status.
    on: hub
    risk: read
    scope: local
    run: scripts/engine.py:status
    result: { type: map }
provides:
  voice.stt: speech-engine.stt
  voice.tts: speech-engine.tts
  voice.turn: speech-engine.turn
  voice.speaker: speech-engine.speaker
"""

ENGINE_PY = """\
import base64


def _text(audio):
    return base64.b64decode(audio["data"]).decode()


def stt(ctx, audio, language=None):
    return {"text": _text(audio).rstrip(".").strip(), "language": "en"}


def tts(ctx, text, voice=None):
    if voice == "broken":
        raise ctx.Error("that voice is down")
    return ctx.file(data=("RIFF:" + (voice or "default") + ":" + text).encode(), name="speech.wav", mime="audio/wav")


def turn(ctx, audio):
    return {"complete": not _text(audio).endswith("...")}


def speaker(ctx, audio):
    return {"label": "bake", "score": 0.81}


def voices(ctx):
    return {"voices": ["af_heart"], "default": "af_heart"}


def status(ctx):
    return {"stt": True, "turn": True}
"""

# System One: greetings it answers itself; everything else goes to the agent.
TALKER = """\
manifest: 1
plugin: llm-openai
version: 1.0.0
doc: A fake System One.
runs_on: hub
caps:
  complete:
    doc: Fake completion.
    on: hub
    risk: read
    scope: external
    run: scripts/talker.py:complete
    args:
      messages:
        type: list
        required: true
        items:
          type: map
          fields:
            role: { type: str, required: true, enum: [system, user, assistant] }
            content: { type: str, required: true }
      model: { type: str }
      max_tokens: { type: int, min: 1 }
      temperature: { type: float, min: 0, max: 2 }
      stop: { type: list, items: { type: str }, max: 4 }
    result:
      type: map
      fields:
        text: { type: str, required: true }
        model: { type: str }
        stop_reason: { type: str }
        usage: { type: map, items: { type: int } }
provides:
  llm.complete: llm-openai.complete
"""

TALKER_PY = """\
import json


def complete(ctx, messages, model=None, max_tokens=None, temperature=None, stop=None):
    said = messages[-1]["content"]
    if said.startswith("hello"):
        plan = {"say": "Hi Bake!" + (" (sounds like bake)" if "sounds like: bake" in messages[0]["content"] else ""),
                "handoff": ""}
    elif said == "mumble":
        return {"text": "I am not JSON"}
    else:
        plan = {"say": "Let me check.", "handoff": said}
    return {"text": "```json\\n" + json.dumps(plan) + "\\n```", "model": model}
"""

BRAIN = """\
manifest: 1
plugin: agents-local
version: 1.0.0
doc: A fake local agent backend.
runs_on: hub
caps:
  turn:
    doc: One fake agent turn.
    risk: exec
    scope: local
    on: hub
    run: scripts/brain.py:turn
    args:
      prompt: { type: str, required: true }
      session: { type: str }
      cwd: { type: str }
      model: { type: str }
      effort: { type: str }
      system: { type: str }
      timeout: { type: duration }
    result:
      type: map
      fields:
        text: { type: str, required: true }
        session: { type: str, required: true }
        usage: { type: map, items: { type: int } }
provides:
  agent.backend: agents-local.turn
"""

BRAIN_PY = """\
def turn(ctx, prompt, session=None, cwd=None, model=None, effort=None, system=None, timeout=None):
    asked = [l for l in prompt.splitlines() if l.startswith("(Spoken")]
    return {"text": "brain: " + (asked[-1] if asked else prompt.splitlines()[-1]), "session": session or "s1"}
"""


def plugin(lib, name, yaml, script_name, script):
    (lib / name / "scripts").mkdir(parents=True)
    (lib / name / "plugin.yaml").write_text(yaml)
    (lib / name / "scripts" / script_name).write_text(script)


def said(text: str) -> dict:
    """An utterance as the fakes take it."""
    return {"data": base64.b64encode(text.encode()).decode(), "mime": "audio/L16"}
