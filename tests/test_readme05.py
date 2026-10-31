"""The front door for 0.5 (castling-first-run): the README leads with asking
your own agent to set Rook up and points it at the setup skill, and every
relative link there and in the skill resolves."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
SKILL = ROOT / "skills" / "rook-setup" / "SKILL.md"
LINK = re.compile(r"\]\(([^)\s]+)\)|src=\"([^\"]+)\"")


def _links(path: Path):
    for m in LINK.finditer(path.read_text(encoding="utf-8")):
        target = (m.group(1) or m.group(2)).split("#", 1)[0]
        if target and not re.match(r"[a-z]+:", target):
            yield target


def test_the_readme_leads_with_asking_your_agent():
    text = README.read_text(encoding="utf-8")
    first = text.index("## ", text.index("# Rook"))
    assert text[first:].startswith("## Getting started")
    assert "The easiest way to get started with Rook is to ask whatever agent you're\nalready using to set it up." \
        in text
    assert "(skills/rook-setup/SKILL.md)" in text
    assert "(docs/manual-0.5/install.md)" in text          # doing it yourself is first-class


def test_the_setup_skill_is_a_skill():
    head = SKILL.read_text(encoding="utf-8").split("---\n")[1]
    assert re.search(r"^name: rook-setup$", head, re.M)
    assert re.search(r"^description: .{40,}", head, re.M)


def test_every_relative_link_resolves():
    missing = [f"{p.relative_to(ROOT)}: {t}" for p in (README, SKILL, ROOT / "docs" / "README-0.4.md")
               for t in _links(p) if not (p.parent / t).exists()]
    assert missing == []
