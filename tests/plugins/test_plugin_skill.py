"""The plugin-writing skill (rook/plugins/agents/skills/plugins.md): its two
examples are pulled out of the file, checked by the real validator and run,
so the skill can't drift from the code."""

import re

import port_helpers
from port_helpers import ME, PLUGINS, go, on_worker
from rook.kit.manifest import _yaml, check

SKILL = PLUGINS / "agents" / "skills" / "plugins.md"
UPOWER = """\
  native-path:          BAT0
    state:               charging
    percentage:          41%
"""


def example(heading: str) -> dict:
    """{file name: content} for each `name`: line followed by a fenced block
    under the ``## <heading>`` section."""
    text = SKILL.read_text(encoding="utf-8")
    section = text.split(f"## {heading}", 1)[1].split("\n## ", 1)[0]
    return dict(re.findall(r"^`([\w./]+)`:\n\n```\w+\n(.*?)^```", section, re.M | re.S))


def write(folder, files: dict):
    for name, content in files.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    raw, issues = check(folder)
    assert raw is not None and not issues, [str(i) for i in issues]
    return raw


def test_the_skill_is_an_agents_skill():
    text = SKILL.read_text(encoding="utf-8")
    head = _yaml.loads(text.split("---\n")[1])
    assert head["description"] and head["when"]
    agents = _yaml.loads((PLUGINS / "agents" / "plugin.yaml").read_text(encoding="utf-8"))
    assert agents["skills"]["plugins"] == "skills/plugins.md"
    assert "plugins" in agents["personas"]["house"]["skills"]
    assert len(text.splitlines()) < 220            # short enough to read in a turn


def test_example_a_a_workers_cap(tmp_path, monkeypatch, bins):
    files = example("Example A")
    assert set(files) == {"plugin.yaml", "scripts/battery.py"}
    lib = tmp_path / "lib"
    raw = write(lib / "battery", files)
    assert raw["caps"]["status"]["on"] == "worker"
    for real in ("shell", "file"):
        (lib / real).symlink_to(PLUGINS / real)
    monkeypatch.setattr(port_helpers, "PLUGINS", lib)
    bins.add("upower", f"print({UPOWER!r})")
    out, seen = on_worker("battery", "battery.status")
    assert out == {"percent": 41.0, "status": "charging"}
    assert [c for c, _ in seen] == ["shell.exec"]


def test_example_b_a_record_type_with_a_page(tmp_path):
    files = example("Example B")
    assert set(files) == {"plugin.yaml", "pages/boards.html"}
    raw = write(tmp_path / "storyboard", files)
    page = raw["pages"]["boards"]
    called = set(re.findall(r'rook\.call\("([\w.]+)"', files["pages/boards.html"]))
    assert called and called <= set(page["caps"])

    from rook.core2.hub.plugins import HubPlugins
    from rook.kit.contracts import Contracts
    from rook.kit.records import RecordStore

    async def body():
        async def call(*a, **kw):
            raise AssertionError("a records-only plugin calls nothing")
        hub = HubPlugins(records=RecordStore(), contracts=Contracts(), call=call)
        await hub.load(tmp_path / "storyboard")
        try:
            made = await hub.run("storyboard.create", {"title": "Opening"}, ME)
            assert made["state"] == "draft"
            assert [b["title"] for b in await hub.run("storyboard.list", {}, ME)] == ["Opening"]
            assert await hub.run("storyboard.search", {"query": "Opening"}, ME)
            assert await hub.run("storyboard.history", {"id": made["id"]}, ME)
        finally:
            await hub.close()
    go(body)

