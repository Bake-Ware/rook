"""Hub._library_folder: a plugin.yaml is parsed once, and again when it changes."""
import os
from types import SimpleNamespace

import yaml

from rook.core2.hub.runtime import Hub


def _hub(lib):
    return SimpleNamespace(cfg=SimpleNamespace(library=lib), _library_names={})


def test_parses_each_manifest_once(tmp_path, monkeypatch):
    for name in ("a", "b", "c"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "plugin.yaml").write_text(f"plugin: {name}-plugin\n", encoding="utf-8")
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "plugin.yaml").write_text("plugin: [\n", encoding="utf-8")
    (tmp_path / "empty").mkdir()
    loads, real = [], yaml.safe_load
    monkeypatch.setattr(yaml, "safe_load", lambda text: (loads.append(1), real(text))[1])
    hub = _hub(tmp_path)
    assert Hub._library_folder(hub, "c-plugin") == tmp_path / "c"
    assert Hub._library_folder(hub, "a-plugin") == tmp_path / "a"
    assert Hub._library_folder(hub, "nope") is None
    assert len(loads) == 4


def test_sees_a_changed_and_a_new_manifest(tmp_path):
    (tmp_path / "a").mkdir()
    path = tmp_path / "a" / "plugin.yaml"
    path.write_text("plugin: one\n", encoding="utf-8")
    hub = _hub(tmp_path)
    assert Hub._library_folder(hub, "one") == tmp_path / "a"
    path.write_text("plugin: other\n", encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert Hub._library_folder(hub, "one") is None
    assert Hub._library_folder(hub, "other") == tmp_path / "a"
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "plugin.yaml").write_text("plugin: one\n", encoding="utf-8")
    assert Hub._library_folder(hub, "one") == tmp_path / "b"


def test_no_library():
    assert Hub._library_folder(_hub(None), "x") is None
