"""Every setting of every in-repo plugin carries a one-line description: the
Settings page is a map people read to learn what the system can do (site
design point 3: "Every setting carries a one-line description")."""

from pathlib import Path

import pytest

from rook.kit.manifest import _yaml

ROOT = Path(__file__).resolve().parents[2]
MANIFESTS = sorted({*ROOT.glob("rook/plugins/*/plugin.yaml"), *ROOT.glob("rook/core2/hub/**/plugin.yaml"),
                    *ROOT.glob("rook/core2/builtin/*.yaml"), *ROOT.glob("worker/manifests/*.yaml")})


def test_the_manifests_are_found():
    assert len(MANIFESTS) > 60


@pytest.mark.parametrize("path", MANIFESTS, ids=lambda p: str(p.relative_to(ROOT)))
def test_every_setting_has_a_one_line_doc(path):
    for key, spec in (_yaml.loads(path.read_text()).get("settings") or {}).items():
        doc = str(spec.get("doc") or "").strip()
        assert doc and "\n" not in doc, f"{key} needs a one-line doc"
