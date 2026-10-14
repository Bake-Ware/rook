#!/usr/bin/env python3
"""Turn pytest's JUnit XML into GitHub Actions error annotations.

    python tools/ci_annotate.py pytest-results.xml

Annotations are readable through the check-runs REST API
(GET /repos/{owner}/{repo}/check-runs/{job_id}/annotations), which works where
the job log download (a redirect to blob storage) is blocked. GitHub keeps 10
error annotations per step, so the first is a list of every failed test and the
next nine carry the first failures' messages.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET


def esc(text: str) -> str:
    # Workflow-command escaping for the message part.
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def esc_prop(text: str) -> str:
    return esc(text).replace(":", "%3A").replace(",", "%2C")


def failures(path: str) -> list[tuple[str, str]]:
    out = []
    for case in ET.parse(path).getroot().iter("testcase"):
        for tag in ("failure", "error"):
            node = case.find(tag)
            if node is None:
                continue
            name = f"{case.get('classname', '')}::{case.get('name', '')}"
            body = (node.get("message") or "") + "\n" + (node.text or "")
            lines = [ln for ln in body.splitlines() if ln.strip()]
            # The tail of a traceback holds the assertion; keep it short.
            out.append((f"{tag.upper()} {name}", "\n".join(lines[-25:])))
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.split("\n\n")[1].strip(), file=sys.stderr)
        return 2
    try:
        found = failures(argv[1])
    except (OSError, ET.ParseError) as e:
        print(f"::error title=pytest::no readable results in {argv[1]}: {esc(str(e))}")
        return 0
    if not found:
        return 0
    print(f"::error title={len(found)} failed test(s)::{esc(chr(10).join(n for n, _ in found))}")
    for name, msg in found[:9]:
        print(f"::error title={esc_prop(name[:200])}::{esc(msg)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
