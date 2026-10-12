"""Shared dashboard JS helpers live once, in rook/web/manage.js."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "rook" / "web"
MODULES = ("bands.js", "sessions.js", "work.js", "knowledge.js", "settings-area.js")


@pytest.mark.parametrize("name", MODULES)
def test_modules_import_the_shared_helpers_instead_of_copying_them(name):
    src = (WEB / name).read_text(encoding="utf-8")
    assert "/account/bands/assets/manage.js" in src
    assert not re.search(r"const (esc|el)\s*=\s*\(?\w", src)   # no private copy
    assert not re.search(r"const ago\s*=\s*\(?t\)?\s*=>\s*\{", src)


def test_index_esc_escapes_single_quotes():
    # index.html is a classic script and cannot import manage.js; its copy must
    # match it, including ' (used inside single-quoted attributes there).
    src = (WEB / "index.html").read_text(encoding="utf-8")
    line = next(ln for ln in src.splitlines() if ln.startswith("function esc("))
    assert "\"'\":'&#39;'" in line and "[&<>\"']" in line


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_helpers_behave(tmp_path):
    (tmp_path / "manage.mjs").write_text((WEB / "manage.js").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "t.mjs").write_text(
        "globalThis.addEventListener=()=>{};"
        "globalThis.document={createElement:t=>({tag:t,textContent:'',className:''})};\n"
        "const m=await import('./manage.mjs');\n"
        "const e=m.el('td','v','c');\n"
        "console.log(JSON.stringify([m.esc(`<a href='x' t=\"y\">&`),m.esc(null),m.ago(0),m.ago(0,''),"
        "m.ago(Date.now()/1000-120),[e.tag,e.textContent,e.className],m.el('b',null).textContent]));\n",
        encoding="utf-8")
    out = json.loads(subprocess.run(["node", str(tmp_path / "t.mjs")], capture_output=True, text=True,
                                    check=True, timeout=30).stdout)
    assert out == ["&lt;a href=&#39;x&#39; t=&quot;y&quot;&gt;&amp;", "", "never", "",
                   "2 min ago", ["td", "v", "c"], ""]
