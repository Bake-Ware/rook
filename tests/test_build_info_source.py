"""A source checkout reports 0.dev+<short commit>, read from .git without git."""

import importlib.util
import shutil
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "rook" / "worker" / "_build_info.py"
SHA = "0123456789abcdef0123456789abcdef01234567"


def _load(repo: Path):
    target = repo / "rook" / "worker" / "_build_info.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(SRC, target)
    spec = importlib.util.spec_from_file_location("build_info_under_test", target)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_no_checkout_is_plain_dev(tmp_path):
    mod = _load(tmp_path)
    assert mod.VERSION == "0.dev" and mod.BUILD == 0


@pytest.mark.parametrize("layout", ["loose", "packed", "detached"])
def test_checkout_layouts(tmp_path, layout):
    git = tmp_path / ".git"
    git.mkdir()
    if layout == "detached":
        (git / "HEAD").write_text(SHA + "\n")
    else:
        (git / "HEAD").write_text("ref: refs/heads/main\n")
        if layout == "loose":
            (git / "refs" / "heads").mkdir(parents=True)
            (git / "refs" / "heads" / "main").write_text(SHA + "\n")
        else:
            (git / "packed-refs").write_text(f"# pack-refs with: peeled\n{SHA} refs/heads/main\n")
    mod = _load(tmp_path)
    assert mod.VERSION == "0.dev+0123456" and mod.BUILD == 0
    assert mod.as_dict()["version"] == mod.VERSION


def test_worktree_reads_refs_from_the_common_dir(tmp_path):
    common = tmp_path / "main" / ".git"
    wt_git = common / "worktrees" / "wt"
    wt_git.mkdir(parents=True)
    (wt_git / "HEAD").write_text("ref: refs/heads/feature\n")
    (wt_git / "commondir").write_text("../..\n")
    (common / "refs" / "heads").mkdir(parents=True)
    (common / "refs" / "heads" / "feature").write_text(SHA + "\n")
    repo = tmp_path / "wt"
    repo.mkdir()
    (repo / ".git").write_text(f"gitdir: {wt_git}\n")
    assert _load(repo).VERSION == "0.dev+0123456"


def test_hub_node_still_parses_build_zero(tmp_path):
    from rook.hub.node import HubNode
    node = HubNode(str(tmp_path), package=None, entry_points=False, build_version="0.dev+0123456")
    assert node.build == 0
