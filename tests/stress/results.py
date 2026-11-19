"""Round 4, large results: calls whose result is 1, 10, 15 and 20 MB (10^6 bytes), as text and as bytes,
through the door. Records what comes back over the 16,000,000-byte message size (the bound stays as
it is). `python tests/stress/results.py`"""

import base64
import json
import os
import time

import rig

SIZES = [1_000_000, 10_000_000, 15_000_000, 20_000_000]


async def body(band):
    w = band.monikers[0]
    files = band.base / "files"
    files.mkdir()
    for n in SIZES:
        (files / f"text-{n}").write_bytes(b"a" * n)
        (files / f"bytes-{n}").write_bytes(os.urandom(n))
    for n in SIZES:
        mb, raw = n // 1_000_000, (files / f"bytes-{n}").read_bytes()
        for label, cap, args, want in (
                ("text, shell.exec stdout", "shell.exec", {"cmd": f"head -c {n} /dev/zero | tr '\\0' a"}, "a" * n),
                ("text, file.read", "file.read", {"path": str(files / f"text-{n}")}, "a" * n),
                ("bytes, file.read base64", "file.read", {"path": str(files / f"bytes-{n}"), "encoding": "base64"},
                 base64.b64encode(raw).decode()),
                ("bytes, shell.exec stdout", "shell.exec", {"cmd": f"cat {files / f'bytes-{n}'}"},
                 raw.decode("utf-8", errors="replace"))):
            sub = f"{mb} MB {label}"
            band.measure("before", sub=sub)
            started = time.monotonic()
            try:
                code, reply, followed = await band.door.call(w, cap, args)
            except Exception as e:
                code, reply, followed = f"no_answer:{type(e).__name__}: {e}"[:200], None, False
            secs = time.monotonic() - started
            if code:
                outcome = {"code": code, "said": str(reply)[:400]}
            else:
                body_ = reply.get("stdout", reply.get("content"))
                outcome = {"code": "ok", "result_chars": len(body_), "reply_json_bytes": len(json.dumps(reply)),
                           "whole": body_ == want}
            band.log("result", sub=sub, seconds=round(secs, 2), followed=followed, **outcome)
            band.measure("after", sub=sub)


if __name__ == "__main__":
    rig.main("results", body, n=1)
