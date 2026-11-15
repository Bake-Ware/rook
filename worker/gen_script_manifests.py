"""Write the manifests rook_worker.py serves into it, as the canonical JSON
the hub hashes (rook.core2.wire.canonical), so the one-file worker needs no
YAML parser and announces the same hashes as the other workers.

    python worker/gen_script_manifests.py          # rewrite the block
    python worker/gen_script_manifests.py --check  # exit 1 when it is stale

tests/test_core2_script_worker.py runs the check.
"""
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from rook.core2.wire import canonical  # noqa: E402

SOURCES = ["rook/core2/builtin/worker.yaml", "rook/plugins/shell/plugin.yaml", "rook/plugins/file/plugin.yaml"]
SCRIPT = ROOT / "worker" / "rook_worker.py"
BEGIN, END = "# -- BEGIN GENERATED MANIFESTS (do not edit) --\n", "# -- END GENERATED MANIFESTS --\n"


def block() -> str:
    lines = [repr(canonical(yaml.safe_load((ROOT / s).read_text(encoding="utf-8"))).decode("utf-8"))
             for s in SOURCES]
    return BEGIN + "MANIFESTS = [\n" + "".join(f"    {line},  # noqa: E501\n" for line in lines) + "]\n" + END


def main() -> int:
    text = SCRIPT.read_text(encoding="utf-8")
    head, rest = text.split(BEGIN, 1)
    new = head + block() + rest.split(END, 1)[1]
    if "--check" in sys.argv:
        return 0 if new == text else 1
    SCRIPT.write_text(new, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
