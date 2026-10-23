"""Scribe: the transcript of a conversation the house voice overheard (2026-10-23).

When several people talk and none of them is talking to the assistant, the voice
stops replying and scribes, but only while the owner is among the speakers (it
never records other people's conversation on its own). Each line is who said it
(an enrolled name, else "Speaker A", "Speaker B" from the voice server's per-
conversation clustering), when, and what. The agent reads the new lines now and
then and files what matters (tasks proposed as usual, never sent to anyone;
takeaways and decisions with the time they were said) on the conversation's
knowledge page; the transcript itself is kept here for ``keep_days`` (30), the
takeaways for good. Raw audio is never kept (the voice server transcribes and
drops it).
"""

from __future__ import annotations

import json
import re
import secrets
import time
from pathlib import Path

ID = re.compile(r"^c[0-9a-f]{8}$")
MAX_LINE = 2000


class Conversations:
    def __init__(self, base: Path) -> None:
        self.base = Path(base)

    def _dir(self) -> Path:
        self.base.mkdir(parents=True, exist_ok=True)
        return self.base

    def _index(self) -> dict:
        try:
            return json.loads((self._dir() / "index.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write_index(self, data: dict) -> None:
        from ....core.db import atomic_write_json
        atomic_write_json(str(self._dir() / "index.json"), data)

    def start(self, *, by: str, page: str = "") -> dict:
        cid = "c" + secrets.token_hex(4)
        rec = {"id": cid, "started": time.time(), "by": by, "page": page, "lines": 0,
               "extracted": 0, "speakers": [], "ended": None}
        idx = self._index()
        idx[cid] = rec
        self._write_index(idx)
        return rec

    def get(self, cid: str) -> dict | None:
        return self._index().get(cid) if ID.match(str(cid or "")) else None

    def update(self, cid: str, **fields) -> dict | None:
        idx = self._index()
        if cid not in idx:
            return None
        idx[cid].update(fields)
        self._write_index(idx)
        return idx[cid]

    def open_one(self) -> dict | None:
        """The conversation being scribed now, if any."""
        live = [r for r in self._index().values() if not r.get("ended")]
        return max(live, key=lambda r: r["started"]) if live else None

    def append(self, cid: str, speaker: str, text: str, at: float | None = None) -> dict:
        rec = self.get(cid)
        if rec is None:
            raise KeyError(cid)
        line = {"n": rec["lines"] + 1, "at": float(at or time.time()),
                "speaker": " ".join(str(speaker or "Speaker").split())[:40],
                "text": " ".join(str(text or "").split())[:MAX_LINE]}
        with open(self._dir() / f"{cid}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")
        speakers = rec["speakers"] if line["speaker"] in rec["speakers"] else \
            rec["speakers"] + [line["speaker"]]
        self.update(cid, lines=line["n"], speakers=speakers[:12], last=line["at"])
        return line

    def lines(self, cid: str, after: int = 0) -> list[dict]:
        path = self._dir() / f"{cid}.jsonl"
        out = []
        try:
            with open(path, encoding="utf-8") as f:
                for raw in f:
                    with_ = json.loads(raw)
                    if with_["n"] > after:
                        out.append(with_)
        except (OSError, ValueError):
            pass
        return out

    def end(self, cid: str) -> dict | None:
        return self.update(cid, ended=time.time())

    def prune(self, keep_days: float) -> list[str]:
        """Delete transcripts older than ``keep_days`` (the records and pages stay)."""
        cutoff = time.time() - float(keep_days) * 86400
        gone = []
        idx = self._index()
        for cid, rec in idx.items():
            if (rec.get("ended") or rec["started"]) < cutoff and not rec.get("pruned"):
                (self._dir() / f"{cid}.jsonl").unlink(missing_ok=True)
                rec["pruned"] = time.time()
                gone.append(cid)
        if gone:
            self._write_index(idx)
        return gone
