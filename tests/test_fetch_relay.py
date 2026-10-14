"""scripts/fetch-relay.py: the download is checked against the committed pin."""

import hashlib
import importlib.util
import io
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fetch_relay", ROOT / "scripts" / "fetch-relay.py")
fr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fr)


def _release(tmp_path, tag, body=b"\x7fELF relay"):
    """A fake releases tree (file:// URLs) holding one x86_64 archive."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("telesthete-hub")
        info.size, info.mode = len(body), 0o755
        tf.addfile(info, io.BytesIO(body))
    data = buf.getvalue()
    d = tmp_path / "releases" / "download" / tag
    d.mkdir(parents=True)
    name = "telesthete-hub-x86_64-linux.tar.gz"
    (d / name).write_bytes(data)
    (d / (name + ".sha256")).write_text(f"{hashlib.sha256(data).hexdigest()}  {name}\n")
    return (tmp_path / "releases").as_uri(), hashlib.sha256(data).hexdigest()


def _pins(tmp_path, tag, sha):
    p = tmp_path / "_release.py"
    p.write_text(f'RELAY = "{tag}"\nRELAY_SHA256 = {{"x86_64": "{sha}"}}\n', encoding="utf-8")
    return p


def test_pinned_download(tmp_path, capsys):
    base, sha = _release(tmp_path, "v0.3.0")
    pins = _pins(tmp_path, "v0.3.0", sha)
    rc = fr.main(["--arch", "x86_64", "--base-url", base, "--pins", str(pins), "--dest", str(tmp_path / "bin")])
    assert rc == 0 and (tmp_path / "bin" / "telesthete-hub").read_bytes() == b"\x7fELF relay"
    assert "WARNING" not in capsys.readouterr().err


def test_a_release_page_cannot_vouch_for_a_changed_archive(tmp_path):
    base, _ = _release(tmp_path, "v0.3.0")
    pins = _pins(tmp_path, "v0.3.0", "0" * 64)    # the pin disagrees with the (replaced) archive
    rc = fr.main(["--arch", "x86_64", "--base-url", base, "--pins", str(pins), "--dest", str(tmp_path / "bin")])
    assert rc == 1 and not (tmp_path / "bin" / "telesthete-hub").exists()


def test_unpinned_version_falls_back_to_the_release_checksum(tmp_path, capsys):
    base, sha = _release(tmp_path, "v0.4.0")
    pins = _pins(tmp_path, "v0.3.0", "0" * 64)
    rc = fr.main(["--version", "v0.4.0", "--arch", "x86_64", "--base-url", base, "--pins", str(pins),
                  "--dest", str(tmp_path / "bin")])
    assert rc == 0 and "no pinned sha256" in capsys.readouterr().err


def test_committed_pin_is_well_formed():
    relay, shas = fr.read_pins(fr.DEFAULT_PINS)
    assert relay.startswith("v") and isinstance(shas, dict)
    assert all(a in fr.ARCHES and len(h) == 64 for a, h in shas.items())
