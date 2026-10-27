"""Voice on a fresh hub without hand setup: the model files download from
Intelligence sources > Voice (and voice then loads, no restart), `rook voice`
installs the extras and the files, and the hosted voice settings reach the
voice service only when someone stores them."""

from __future__ import annotations

import json
import textwrap

import pytest

from rook.cli import voice as cli
from rook.hub.plugins import voice as voice_mod
from rook.hub.plugins.voice import engine as eng
from services.voice import fetch_models
from tests.test_voice_plugin import (clean_env, fake_engine, gpu_node, node_for,  # noqa: F401
                                     wait_for)

FAKE_FETCH = '''
import os, threading
from pathlib import Path
NAMES = {names!r}
GATE = threading.Event()
def manifest():
    return {{n: {{"size": 10, "required": True}} for n in NAMES}}
def missing(model_dir, names=None):
    return [n for n in NAMES if not (Path(model_dir) / n).is_file()]
def fetch(model_dir, names=None, progress=None):
    GATE.wait(5)
    if os.environ.get("FAKE_FETCH_FAIL"):
        raise ValueError("network down")
    Path(model_dir).mkdir(parents=True, exist_ok=True)
    got = missing(model_dir)
    for n in got:
        if progress:
            progress(n, 10, 10)
        (Path(model_dir) / n).write_bytes(b"weights")
    return got
'''


def test_the_hub_and_the_manifest_agree_on_the_model_files():
    assert list(eng.MODEL_FILES) == fetch_models.required()


@pytest.fixture
def no_models(fake_engine):
    """The fake voice service, with a fetch_models module and no model files."""
    import importlib
    pkg = fake_engine.models.parent / "pkgs" / "fakevoice"
    (pkg / "fetch_models.py").write_text(textwrap.dedent(FAKE_FETCH.format(names=list(eng.MODEL_FILES))))
    for name in eng.MODEL_FILES:
        (fake_engine.models / name).unlink()
    importlib.invalidate_caches()
    voice_mod.DOWNLOAD = voice_mod.ModelDownload()
    yield fake_engine
    voice_mod.DOWNLOAD = voice_mod.ModelDownload()


@pytest.mark.asyncio
async def test_download_on_the_voice_page_then_voice_loads(tmp_path, no_models):
    node = node_for(tmp_path, model_dir=str(no_models.models))
    assert node.plugin("voice") is None
    await node.start()
    page = voice_mod.page_data(node.settings, node, None)
    assert page["requirements"]["models_missing"] == list(eng.MODEL_FILES)
    assert "Download them on Intelligence sources > Voice" in page["status"]["reason"]
    assert page["download"]["state"] == "idle"

    import sys
    started = voice_mod.download_models(node.settings, node, None)
    assert started["state"] == "running" and started["total"] == 30
    assert started["model_dir"] == str(no_models.models)
    # A second press while it runs starts nothing new.
    assert voice_mod.download_models(node.settings, node, None)["state"] == "running"
    sys.modules["fakevoice.fetch_models"].GATE.set()
    assert await wait_for(lambda: voice_mod.DOWNLOAD.state == "done")
    assert all((no_models.models / n).is_file() for n in eng.MODEL_FILES)
    assert voice_mod.DOWNLOAD.done == 30
    # Voice was held back only for the files: it loads now, without a restart.
    try:
        assert await wait_for(lambda: node.plugin("voice") is not None), node.host.status["voice"]
    finally:
        await node.stop()


@pytest.mark.asyncio
async def test_a_failed_download_says_why(tmp_path, no_models, monkeypatch):
    monkeypatch.setenv("FAKE_FETCH_FAIL", "1")
    node = node_for(tmp_path, model_dir=str(no_models.models))
    import sys
    voice_mod.download_models(node.settings, node, None)
    sys.modules["fakevoice.fetch_models"].GATE.set()
    assert await wait_for(lambda: voice_mod.DOWNLOAD.state == "error")
    snap = voice_mod.page_data(node.settings, node, None)["download"]
    assert snap["error"] == "network down" and snap["finished_at"]
    assert node.plugin("voice") is None


def test_no_download_when_speech_runs_elsewhere(tmp_path, no_models):
    node = node_for(tmp_path, engine="remote", remote_url="http://192.0.2.7:8900")
    with pytest.raises(ValueError, match="another machine"):
        voice_mod.download_models(node.settings, node, node.plugin("voice"))


# -- rook voice ------------------------------------------------------------------------------
def test_voice_extras_come_from_rooks_own_voice_extra():
    reqs = cli.voice_extras()
    names = {r.split(">")[0].split("=")[0].split("<")[0] for r in reqs}
    assert {"faster-whisper", "kokoro-onnx", "onnxruntime", "webrtcvad", "fastapi"} <= names


def test_rook_voice_status_and_models(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(eng, "missing_extras", lambda: [])
    monkeypatch.delenv("VOICE_MODEL_DIR", raising=False)
    monkeypatch.setenv("ROOK_DATA_DIR", str(tmp_path))
    d = tmp_path / "plugins" / "voice"
    assert cli.main(["status"]) == 1
    out = capsys.readouterr().out
    assert str(d) in out and "missing kokoro-v1.0.onnx" in out and "rook voice setup" in out
    fetched = []
    monkeypatch.setattr(fetch_models, "main", lambda argv: fetched.append(argv) or 0)
    assert cli.main(["setup"]) == 0
    assert fetched == [[str(d)]]
    assert "voice extras: already installed" in capsys.readouterr().out
    assert cli.main(["models", "--model-dir", "/x", "--check"]) == 0 and fetched[-1] == ["/x", "--check"]


def test_rook_voice_setup_installs_missing_extras(tmp_path, monkeypatch):
    calls, missing = [], [["faster-whisper"], []]
    monkeypatch.setattr(eng, "missing_extras", lambda: missing.pop(0) if len(missing) > 1 else missing[0])
    monkeypatch.setattr(cli.subprocess, "call", lambda argv: calls.append(argv) or 0)
    monkeypatch.setattr(fetch_models, "main", lambda argv: 0)
    assert cli.main(["setup", "--model-dir", str(tmp_path)]) == 0
    assert calls and calls[0][1:4] == ["-m", "pip", "install"] and any("kokoro-onnx" in a for a in calls[0])


def test_no_model_folder_is_a_plain_error(monkeypatch):
    monkeypatch.delenv("VOICE_MODEL_DIR", raising=False)
    monkeypatch.delenv("ROOK_DATA_DIR", raising=False)
    with pytest.raises(SystemExit, match="no model folder"):
        cli.main(["status"])


# -- hosted voices ----------------------------------------------------------------------------
def test_hosted_voices_are_off_by_default(tmp_path, fake_engine):
    node = gpu_node(tmp_path, model_dir=str(fake_engine.models))
    voice = node.plugin("voice")
    assert voice.settings.get("tts_hosted") == "off"
    env = voice.engine_env()
    assert not {k for k in env if "ELEVENLABS" in k or k == "VOICE_TTS_HOSTED"}
    from rook.hub.settings_schema import Schema
    s = Schema()
    assert s.get("voice.elevenlabs_api_key").setting.secret
    assert s.get("voice.tts_hosted").setting.choices == ("off", "elevenlabs")
    assert json.loads(json.dumps(voice_mod.ENGINE_ORDER))["elevenlabs"] == 2
