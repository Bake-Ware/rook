"""The agent skill: generated reference freshness, packaging, serving, install."""
import importlib.util
import io
import re
import zipfile
from pathlib import Path

import httpx
import pytest

from rook.band_mcp import skill
from rook.band_mcp.server import build_server
from rook.cli import skill as skill_cli

ROOT = Path(__file__).resolve().parent.parent
STATIC = "static-token-0123456789abcdef"


def _gen():
    spec = importlib.util.spec_from_file_location("gen_skill_reference",
                                                  ROOT / "tools" / "gen_skill_reference.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_committed_tools_reference_is_current():
    # Fails when a tool or cap changed without re-running the generator:
    #   python tools/gen_skill_reference.py
    assert _gen().main(["--check"]) == 0


def test_reference_covers_every_tool_and_plugin_cap():
    text = (ROOT / "skills/rook/references/tools.md").read_text()
    for name in ("rook_call", "rook_knowledge", "rook_task", "rook_console_open"):
        assert f"`{name}`" in text
    for cap in ("shell.exec", "file.read", "customcap.add", "caps.describe", "worker.apply"):
        assert f"`{cap}`" in text


def test_skill_files_are_complete_and_generic():
    files = skill.base_files()
    assert {"SKILL.md", "references/install.md", "references/admin.md",
            "references/tools.md", "references/usage.md"} <= set(files)
    assert skill.SITE_FILE not in files
    head = files["SKILL.md"].split("---")[1]
    assert re.search(r"^name: rook$", head, re.M) and "description:" in head
    for ref in re.findall(r"`(references/[\w.-]+\.md)`", files["SKILL.md"]):
        assert ref in files or ref == skill.SITE_FILE, ref
    blob = "\n".join(files.values())
    assert not re.search(r"/home/[a-z]", blob)
    assert not re.search(r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d+\.\d+", blob)


def test_package_is_a_claude_skill_zip_and_deterministic():
    data = skill.package(skill.files("# Site\nnotes\n"))
    assert data == skill.package(skill.files("# Site\nnotes\n"))
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
    assert "rook/SKILL.md" in names and "rook/references/site.md" in names
    assert all(n.startswith("rook/") for n in names)
    assert skill_cli.unpack(data)["references/site.md"] == "# Site\nnotes\n"


@pytest.mark.parametrize("bad", ["rook/../evil.md", "other/SKILL.md", "rook/run.sh", "/rook/x.md"])
def test_unpack_refuses_unexpected_entries(bad):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("rook/SKILL.md", "x")
        z.writestr(bad, "x")
    with pytest.raises(ValueError):
        skill_cli.unpack(buf.getvalue())


def test_install_writes_and_clears_stale_site_notes(tmp_path):
    dest = tmp_path / "rook"
    skill.install(skill.files("site"), dest)
    assert (dest / "references/site.md").read_text() == "site"
    skill.install(skill.files(), dest)
    assert (dest / "SKILL.md").is_file() and not (dest / "references/site.md").exists()


def test_harness_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert skill_cli.harness_dir("claude") == tmp_path / ".claude/skills/rook"
    assert skill_cli.harness_dir("codex") == tmp_path / ".agents/skills/rook"


def test_cli_install_and_package(tmp_path, capsys):
    assert skill_cli.main(["install", "--dest", str(tmp_path / "rook")]) == 0
    assert (tmp_path / "rook/references/tools.md").is_file()
    out = tmp_path / "rook.skill"
    assert skill_cli.main(["package", "-o", str(out)]) == 0
    assert "SKILL.md" in skill_cli.unpack(out.read_bytes())


def test_cli_install_from_hub_sends_token(tmp_path, monkeypatch):
    seen = {}
    payload = skill.package(skill.files("from hub"))

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        seen["url"], seen["auth"] = req.full_url, req.get_header("Authorization")
        return Resp(payload)

    monkeypatch.setattr(skill_cli.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("ROOK_TOKEN", "tok")
    assert skill_cli.main(["install", "--hub", "https://hub.example.com/",
                           "--dest", str(tmp_path / "rook")]) == 0
    assert seen == {"url": "https://hub.example.com/skill/rook.skill", "auth": "Bearer tok"}
    assert (tmp_path / "rook/references/site.md").read_text() == "from hub"


def test_site_notes_from_knowledge_page(monkeypatch):
    class Store:
        def get(self, band, rid):
            assert (band, rid) == ("b1", "site-notes")
            return {"title": "Our band", "body": "worker-a builds things"}

    class Knowledge:
        store = Store()

        def _band_for(self, band, rid):
            return "b1"

    monkeypatch.setenv("ROOK_SKILL_SITE_PAGE", "site-notes")
    monkeypatch.delenv("ROOK_SKILL_SITE_FILE", raising=False)
    assert skill.site_notes(Knowledge()) == "# Our band\n\nworker-a builds things\n"
    assert skill.site_notes(None) is None  # knowledge disabled → no overlay, no error


class FakeBand:
    workers: dict = {}

    async def call(self, *a, **k):
        return {"ok": True, "result": {}}


@pytest.mark.asyncio
async def test_hub_serves_skill_as_resources_and_download(tmp_path, monkeypatch):
    site = tmp_path / "site.md"
    site.write_text("# Site\nworker-a is the build box\n")
    monkeypatch.setenv("ROOK_SKILL_SITE_FILE", str(site))
    monkeypatch.delenv("ROOK_SKILL_SITE_PAGE", raising=False)
    mcp, _ = build_server(FakeBand(), persist_path=str(tmp_path / "tokens.json"),
                          static_token=STATIC, journal_path=str(tmp_path / "journal.db"))

    uris = {str(r.uri) for r in await mcp.list_resources()}
    assert {"rook://skill/rook", "rook://skill/rook/references/tools.md",
            "rook://skill/rook/references/site.md"} <= uris
    body = list(await mcp.read_resource("rook://skill/rook"))[0].content
    assert body.startswith("---\nname: rook")
    notes = list(await mcp.read_resource("rook://skill/rook/references/site.md"))[0].content
    assert "build box" in notes

    app = mcp.streamable_http_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://127.0.0.1:8765") as http:
        anon = await http.get("/skill/rook.skill")
        assert anon.status_code == 200 and anon.headers["content-type"] == "application/zip"
        assert "references/site.md" not in skill_cli.unpack(anon.content)
        authed = await http.get("/skill/rook.skill", headers={"Authorization": "Bearer " + STATIC})
        assert "build box" in skill_cli.unpack(authed.content)["references/site.md"]
        wrong = await http.get("/skill/rook.skill", headers={"Authorization": "Bearer nope"})
        assert "references/site.md" not in skill_cli.unpack(wrong.content)
        one = await http.get("/skill/rook/references/usage.md")
        assert one.status_code == 200 and one.text.startswith("# Usage")
        assert (await http.get("/skill/rook/references/site.md")).status_code == 404
        assert (await http.get("/skill/rook/nope.md")).status_code == 404
