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


# Dashboard modules are versioned (?v=<content hash>) only where the server
# rewrites a literal URL in index.html (bootstrap._versioned_assets). A module
# that imports manage.js by bare URL can be paired with a stale cached copy
# after a deploy, so each one passes its own ?v= on, the way work.js versions
# sessions.js.
_STATIC_IMPORT = re.compile(r"""^\s*import\s[^;]*?\bfrom\s*['"](/account/[^'"]+)['"]""", re.M)
_DYNAMIC_IMPORT = re.compile(r"""\bimport\(\s*(['"])(/account/[^'"]+)\1\s*(\+\s*([^)]*))?\)""")


def _js_modules():
    return sorted(p for p in WEB.glob("*.js") if p.name != "manage.js")


@pytest.mark.parametrize("path", _js_modules(), ids=lambda p: p.name)
def test_every_dashboard_import_is_versioned(path):
    src = path.read_text(encoding="utf-8")
    assert not _STATIC_IMPORT.findall(src), \
        f"{path.name}: static import of an /account/ asset cannot carry ?v=; use await import(url + search)"
    for m in _DYNAMIC_IMPORT.finditer(src):
        url, suffix = m.group(2), (m.group(4) or "")
        if "/vendor/" in url:
            continue  # third-party files, versioned by their own path
        assert "import.meta.url" in suffix or re.fullmatch(r"\s*search\s*", suffix), \
            f"{path.name}: import('{url}') is not versioned"


def test_manage_js_is_reached_only_through_versioned_urls():
    hits = [p.name for p in _js_modules() if "/account/bands/assets/manage.js" in p.read_text(encoding="utf-8")]
    assert len(hits) >= 10   # every dashboard module still uses the shared helpers


def test_index_and_band_page_version_their_module_urls(tmp_path):
    from rook.remote.bootstrap import WEB_DIR, _versioned_assets
    html = _versioned_assets((WEB / "index.html").read_text(encoding="utf-8"), WEB_DIR)
    bare = re.findall(r"import\('(/account/[a-z]+/assets/[\w.-]+\.js)'\)", html)
    assert bare == [], f"index.html imports left unversioned: {bare}"
    assert "import('/account/bands/assets/'+view" not in html  # built URLs escape the rewrite


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("path", sorted(WEB.glob("*.js")), ids=lambda p: p.name)
def test_dashboard_modules_parse(path):
    subprocess.run(["node", "--check", str(path)], check=True, capture_output=True, timeout=30)
