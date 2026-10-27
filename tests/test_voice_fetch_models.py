"""services/voice/fetch_models.py: the speech model files, downloaded and checked
against models.json, so a fresh install needs no hand setup. No network: a fake
opener serves the files."""
import hashlib
import io
import json

import pytest

from services.voice import fetch_models as fm


def test_the_manifest_pins_every_required_file():
    entries = fm.manifest()
    assert fm.required(entries) == ["kokoro-v1.0.onnx", "voices-v1.0.bin", "smart-turn-v3.2-cpu.onnx"]
    for name in fm.required(entries):
        e = entries[name]
        assert e["url"].startswith("https://") and len(e["sha256"]) == 64 and e["size"] > 0, name
    # The speaker model stays optional (speaker ID learning has its own setup).
    assert "nemo_en_titanet_small.onnx" in entries and not entries["nemo_en_titanet_small.onnx"].get("required")


class Served:
    def __init__(self, files, lie=()):
        self.files, self.lie, self.asked = files, set(lie), []

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.asked.append(url)
        data = self.files[url]
        if url in self.lie:
            data = data[:-1] + b"!"
        body = io.BytesIO(data)
        body.headers = {"Content-Length": str(len(data))}
        return _Ctx(body)


class _Ctx:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self.body

    def __exit__(self, *a):
        return False


@pytest.fixture
def manifest(tmp_path, monkeypatch):
    files = {"a.onnx": b"A" * (3 << 20), "b.bin": b"bbb", "opt.onnx": b"optional"}
    entries = {}
    for name, data in files.items():
        entries[name] = {"url": f"https://models.example/{name}", "sha256": hashlib.sha256(data).hexdigest(),
                         "size": len(data)}
        if name != "opt.onnx":
            entries[name]["required"] = True
    path = tmp_path / "models.json"
    path.write_text(json.dumps(entries))
    monkeypatch.setattr(fm, "MANIFEST", path)
    return Served({e["url"]: files[n] for n, e in entries.items()}), files


def test_fetch_downloads_the_missing_required_files(tmp_path, manifest):
    served, files = manifest
    d = tmp_path / "models"
    seen = []
    assert fm.fetch(d, progress=lambda n, done, total: seen.append((n, done, total)), opener=served) == [
        "a.onnx", "b.bin"]
    assert (d / "a.onnx").read_bytes() == files["a.onnx"] and (d / "b.bin").read_bytes() == b"bbb"
    assert not (d / "opt.onnx").exists() and not list(d.glob("*.part"))
    assert seen[-1] == ("b.bin", 3, 3) and ("a.onnx", 3 << 20, 3 << 20) in seen
    # Again: nothing to do, nothing fetched.
    served.asked.clear()
    assert fm.fetch(d, opener=served) == [] and served.asked == []
    assert fm.missing(d) == [] and fm.verify(d) == {}


def test_a_file_that_does_not_match_is_never_kept(tmp_path, manifest):
    served, _ = manifest
    served.lie.add("https://models.example/b.bin")
    d = tmp_path / "models"
    with pytest.raises(ValueError, match="b.bin: downloaded file does not match"):
        fm.fetch(d, opener=served)
    assert (d / "a.onnx").is_file()                 # finished before the bad one: kept
    assert not (d / "b.bin").exists() and not list(d.glob("*.part"))
    assert fm.missing(d) == ["b.bin"]


def test_only_https(tmp_path, manifest):
    entries = fm.manifest()
    entries["a.onnx"]["url"] = "http://models.example/a.onnx"
    fm.MANIFEST.write_text(json.dumps(entries))
    with pytest.raises(ValueError, match="non-https"):
        fm.fetch(tmp_path / "m", ["a.onnx"], opener=manifest[0])
    with pytest.raises(ValueError, match="not in models.json"):
        fm.fetch(tmp_path / "m", ["nope"], opener=manifest[0])


def test_check_reports_missing_and_tampered_files(tmp_path, manifest, capsys, monkeypatch):
    d = tmp_path / "models"
    d.mkdir()
    assert fm.main([str(d), "--check"]) == 1
    assert "missing  a.onnx" in capsys.readouterr().out
    monkeypatch.setattr(fm.urllib.request, "urlopen", manifest[0])
    monkeypatch.setattr(fm.fetch, "__defaults__", (None, None, manifest[0]))
    assert fm.main([str(d), "--optional"]) == 0
    assert (d / "opt.onnx").is_file()
    assert fm.main([str(d), "--check"]) == 0
    (d / "b.bin").write_bytes(b"changed")
    assert fm.main([str(d), "--check"]) == 1
    assert "bad      b.bin" in capsys.readouterr().out


def test_not_enough_disk_space_downloads_nothing(tmp_path, manifest, monkeypatch):
    from collections import namedtuple
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(fm.shutil, "disk_usage", lambda p: usage(1 << 30, 1 << 30, 10 << 20))
    served, _ = manifest
    with pytest.raises(OSError, match="not enough disk space"):
        fm.fetch(tmp_path / "m", opener=served)
    assert served.asked == []
