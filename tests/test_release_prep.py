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
    src = rp.release_module("0.2.0", 523, "abc1234", "523.lumpy.moose")
    (tmp_path / "rook").mkdir()
    (tmp_path / "rook" / "_release.py").write_text(src, encoding="utf-8")
    info = runpy.run_path(str(tmp_path / "rook" / "_release.py"))
    assert (info["VERSION"], info["BUILD"], info["COMMIT"], info["NAME"]) == \
        ("0.2.0", 523, "abc1234", "523.lumpy.moose")
    # The relay pin is committed, not stamped: stamping keeps it.
    assert info["RELAY"] == runpy.run_path(str(ROOT / "rook" / "_release.py"))["RELAY"]
    # The committed defaults are "not a release".
    assert bbw.release_stamp(ROOT) is None


H1, H2, H3 = "1" * 64, "2" * 64, "3" * 64


def test_relay_pin_round_trips_and_refuses_a_changed_archive():
    src = (ROOT / "rook" / "_release.py").read_text(encoding="utf-8")
    pinned = rp.set_relay_pin(src, "v0.4.0", {"x86_64": H1, "aarch64": H2})
    assert rp.current_relay_pin(pinned) == ("v0.4.0", {"x86_64": H1, "aarch64": H2})
    again = rp.set_relay_pin(pinned, "v0.4.0", {"x86_64": H1, "aarch64": H2, "armv7": H3})
    assert rp.current_relay_pin(again)[1]["armv7"] == H3
    with pytest.raises(ValueError, match="changed since"):
        rp.set_relay_pin(again, "v0.4.0", {"x86_64": H3, "aarch64": H2})
    # A new tag may have new hashes; a pin needs both main arches.
    assert rp.current_relay_pin(rp.set_relay_pin(again, "v0.5.0", {"x86_64": H3, "aarch64": H3}))[0] == "v0.5.0"
    with pytest.raises(ValueError):
        rp.set_relay_pin(src, "v0.4.0", {"x86_64": H1})
    with pytest.raises(ValueError):
        rp.set_relay_pin(src, "latest", {"x86_64": H1, "aarch64": H2})


def test_relay_hashes_come_from_the_archives(tmp_path):
    import hashlib
    for arch, body in (("x86_64", b"a"), ("aarch64", b"b")):
        name = rp.relay_asset(arch)
        (tmp_path / name).write_bytes(body)
        (tmp_path / (name + ".sha256")).write_text(f"{hashlib.sha256(body).hexdigest()}  {name}\n")
    shas = rp.relay_hashes("v0.4.0", tmp_path)
    assert shas == {"x86_64": hashlib.sha256(b"a").hexdigest(), "aarch64": hashlib.sha256(b"b").hexdigest()}
    (tmp_path / rp.relay_asset("aarch64")).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="does not match"):
        rp.relay_hashes("v0.4.0", tmp_path)


def test_dockerfile_relay_default_matches_the_pin():
    """The image and the installer must run the same relay."""
    relay, _ = rp.current_relay_pin((ROOT / "rook" / "_release.py").read_text(encoding="utf-8"))
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert f"ARG TELESTHETE_RELAY_VERSION={relay}\n" in docker
    assert "ARG TELESTHETE_RELAY_VERSION=v9.9.9" in rp.set_dockerfile_relay(docker, "v9.9.9")


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
