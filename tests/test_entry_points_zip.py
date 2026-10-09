"""Plugin discovery must not keep a zip on sys.path open (Windows OTA swap)."""
import os
import sys
import zipfile

from rook.core.plugin import iter_entry_points


def _open_paths():
    fd_dir = "/proc/self/fd"
    out = []
    for fd in os.listdir(fd_dir):
        try:
            out.append(os.readlink(os.path.join(fd_dir, fd)))
        except OSError:
            pass
    return out


def test_zip_on_sys_path_is_not_held_open(tmp_path, monkeypatch):
    bundle = tmp_path / "band-worker.pyz"
    with zipfile.ZipFile(bundle, "w") as z:
        z.writestr("__main__.py", "")
        z.writestr("demo-1.0.dist-info/METADATA", "Name: demo\nVersion: 1.0\n")
        z.writestr("demo-1.0.dist-info/entry_points.txt", "[rook.plugins]\ndemo = demo:PLUGIN\n")
    monkeypatch.setattr(sys, "path", [str(bundle), *sys.path])
    names = [c.module for c in iter_entry_points()]
    assert "demo" not in names
    if os.path.isdir("/proc/self/fd"):
        assert str(bundle) not in _open_paths()


def test_directory_entry_points_still_found(tmp_path, monkeypatch):
    info = tmp_path / "demo-1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Name: demo\nVersion: 1.0\n")
    (info / "entry_points.txt").write_text("[rook.plugins]\ndemo = demo:PLUGIN\n")
    monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])
    assert [c.module for c in iter_entry_points()].count("demo") == 1
