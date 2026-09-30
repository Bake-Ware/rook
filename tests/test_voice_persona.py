"""The voice assistant's name and owner phrasing come from the environment."""
import importlib
import sys
import types


def _providers(monkeypatch, **env):
    for mod in ("numpy", "faster_whisper", "kokoro_onnx"):
        if mod not in sys.modules:
            try:
                importlib.import_module(mod)
            except ImportError:
                stub = types.ModuleType(mod)
                stub.WhisperModel = stub.Kokoro = object
                monkeypatch.setitem(sys.modules, mod, stub)
    for key in ("ROOK_VOICE_ASSISTANT_NAME", "ROOK_VOICE_OWNER"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    import services.voice.providers as providers
    return importlib.reload(providers)


def _restore(monkeypatch, providers):
    """Leave the module with default (env-free) values for later tests."""
    for key in ("ROOK_VOICE_ASSISTANT_NAME", "ROOK_VOICE_OWNER"):
        monkeypatch.delenv(key, raising=False)
    importlib.reload(providers)


def _descriptions(providers):
    return {t["function"]["name"]: t["function"]["description"] for t in providers.TOOLS}


def test_defaults_are_neutral(monkeypatch):
    p = _providers(monkeypatch)
    try:
        assert p.MOUTHPIECE_SYSTEM.startswith("You are Rook, the user's personal voice assistant. Speak")
        tools = _descriptions(p)
        assert "the user's Rook band" in tools["rook_devices"]
        assert "the user's own systems" in tools["web_search"]
    finally:
        _restore(monkeypatch, p)


def test_name_and_owner_from_env(monkeypatch):
    p = _providers(monkeypatch, ROOK_VOICE_ASSISTANT_NAME="Ada", ROOK_VOICE_OWNER="Alex")
    try:
        assert p.MOUTHPIECE_SYSTEM.startswith("You are Ada, Alex's personal voice assistant.")
        assert "Alex's Rook band" in _descriptions(p)["rook_devices"]
        assert p.owner_possessive("Chris") == "Chris'"
        assert p.owner_possessive("  ") == "the user's"
    finally:
        _restore(monkeypatch, p)
