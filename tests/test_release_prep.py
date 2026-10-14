"""tools/release_prep.py: tag -> package version, release stamp, telesthete pin."""

import importlib.util
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release_prep", ROOT / "tools" / "release_prep.py")
rp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rp)

from rook.remote import build_band_worker as bbw  # noqa: E402

SHA = "ab" * 32
URL = "https://github.com/Bake-Ware/rook/releases/download/v0.2.0/telesthete-0.3.0-py3-none-any.whl"


@pytest.mark.parametrize("tag,version", [
    ("v0.2.0", "0.2.0"), ("refs/tags/v1.10.3", "1.10.3"), ("v0.3.0-rc1", "0.3.0rc1"),
    ("v0.3.0rc2", "0.3.0rc2"), ("v2.0", "2.0"), ("v1.0.0.post1", "1.0.0.post1"),
])
def test_version_from_tag(tag, version):
    assert rp.version_from_tag(tag) == version


@pytest.mark.parametrize("tag", ["0.2.0", "vfoo", "v1.2.3-beta", "v1.2.3+local", "510.lumpy.moose"])
def test_version_from_tag_rejects(tag):
    with pytest.raises(ValueError):
        rp.version_from_tag(tag)


def test_rewrites_the_real_pyproject_and_init():
    py = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    out = rp.set_project_version(py, "0.2.0")
    assert '\nversion = "0.2.0"\n' in out
    out = rp.set_telesthete_requirement(out, URL, SHA)
    assert f'"telesthete @ {URL}#sha256={SHA}"' in out and "git+" not in out.split("[project.optional")[0]
    # The rest of the file is untouched.
    assert out.count("\n") == py.count("\n")
    init = rp.set_init_version((ROOT / "rook" / "__init__.py").read_text(encoding="utf-8"), "0.2.0")
    assert '__version__ = "0.2.0"' in init


@pytest.mark.parametrize("url,sha", [(URL, "AB" * 32), (URL, "ab" * 31), (URL.replace("https", "http"), SHA),
                                     (URL[:-4] + ".tar.gz", SHA)])
def test_telesthete_requirement_is_validated(url, sha):
    with pytest.raises(ValueError):
        rp.set_telesthete_requirement('"telesthete @ git+https://x/y@abc1234"', url, sha)


def test_release_module_round_trips(tmp_path):
    src = rp.release_module("0.2.0", 523, "abc1234", "523.lumpy.moose", "v0.3.0")
    (tmp_path / "rook").mkdir()
    (tmp_path / "rook" / "_release.py").write_text(src, encoding="utf-8")
    info = runpy.run_path(str(tmp_path / "rook" / "_release.py"))
    assert (info["VERSION"], info["BUILD"], info["COMMIT"], info["NAME"], info["RELAY"]) == \
        ("0.2.0", 523, "abc1234", "523.lumpy.moose", "v0.3.0")
    with pytest.raises(ValueError):
        rp.release_module("0.2.0", 523, "abc1234", "x", "latest")
    # The committed defaults are "not a release".
    assert bbw.release_stamp(ROOT) is None


def test_wheel_install_bundles_carry_the_release_build(tmp_path, monkeypatch):
    """No .git beside the package (a pip install): compute_build uses the stamp."""
    (tmp_path / "rook").mkdir()
    (tmp_path / "rook" / "_release.py").write_text(
        rp.release_module("0.2.0", 523, "abc1234", "x"), encoding="utf-8")
    monkeypatch.setattr(bbw, "REPO_ROOT", tmp_path)
    build, commit, version = bbw.compute_build()
    assert (build, commit) == (523, "abc1234")
    assert version == f"523.{bbw.build_name('abc1234')}"


def test_unstamped_install_still_builds_zero(tmp_path, monkeypatch):
    (tmp_path / "rook").mkdir()
    (tmp_path / "rook" / "_release.py").write_text(
        (ROOT / "rook" / "_release.py").read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(bbw, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(bbw, "_git", lambda *a: (_ for _ in ()).throw(OSError("no git")))
    assert bbw.compute_build() == (0, "", "0.dev")


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_git_build_refuses_a_dirty_tree(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "f").write_text("1")
    _git(tmp_path, "add", "f")
    _git(tmp_path, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "one")
    build, commit = rp.git_build(tmp_path)
    assert build == 1 and len(commit) >= 7
    (tmp_path / "f").write_text("2")
    with pytest.raises(RuntimeError, match="clean checkout"):
        rp.git_build(tmp_path)


def test_print_telesthete_pin():
    out = subprocess.run([sys.executable, str(ROOT / "tools" / "release_prep.py"), "--print-telesthete-pin"],
                         capture_output=True, text=True, check=True).stdout.strip()
    assert out == bbw.pinned_telesthete_rev(ROOT / "pyproject.toml")
    assert len(out) == 40
