"""Text turns through a voice server with the house engine (docs/design/persistent-agents.md).

    python voice_text_check.py [--url wss://127.0.0.1:8931/ws] [--insecure] [turn text ...]

Opens a front_background conversation (no audio: ``speak: false``), sends each
text as a turn, and prints the reply, the time to the first reply text and to
the end of the turn, and the engine the server reports. Uses no voice key
unless ``VOICE_TOKEN`` is set in the environment (never printed). Run beside a
candidate started with ``VOICE_ENGINE=house`` to check the engine end to end.
"""
import argparse
import asyncio
import json
import os
import ssl
import time
import uuid

import websockets

DEFAULT_TURNS = ["Hi, quick check: can you hear me?",
                 "What's two plus two?",
                 "Ask the house agent what the overnight push was about, in one sentence."]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="wss://127.0.0.1:8931/ws")
    ap.add_argument("--insecure", action="store_true")
    ap.add_argument("--wait-push", type=float, default=45.0,
                    help="after the last turn, wait this long for back-end results")
    ap.add_argument("turns", nargs="*")
    a = ap.parse_args()
    tls = None
    if a.url.startswith("wss://") and a.insecure:
        tls = ssl.create_default_context()
        tls.check_hostname = False
        tls.verify_mode = ssl.CERT_NONE
    headers = {"Authorization": "Bearer " + os.environ["VOICE_TOKEN"]} \
        if os.environ.get("VOICE_TOKEN") else {}
    ws = await websockets.connect(a.url, additional_headers=headers,
                                  **({"ssl": tls} if tls else {}))
    await ws.send(json.dumps({"type": "hello", "protocol": 2, "conversation": str(uuid.uuid4()),
                              "pipeline": "front_background", "activity": True}))
    t_end = time.monotonic() + 20
    while time.monotonic() < t_end:
        ev = await ws.recv()
        if isinstance(ev, str) and json.loads(ev).get("type") == "session":
            print("session:", json.dumps({k: v for k, v in json.loads(ev).items()
                                          if k in ("engine", "pipeline", "mode")}))
            break
    for text in a.turns or DEFAULT_TURNS:
        t0 = time.monotonic()
        first = None
        parts = []
        await ws.send(json.dumps({"type": "text", "text": text, "speak": False}))
        async with asyncio.timeout(90):
            while True:
                raw = await ws.recv()
                if isinstance(raw, bytes):
                    continue
                ev = json.loads(raw)
                if ev.get("type") == "error":
                    raise SystemExit(f"error: {ev.get('msg')}")
                if ev.get("type") == "assistant_delta":
                    first = first or time.monotonic() - t0
                    parts.append(ev.get("text") or "")
                if ev.get("type") == "assistant_done":
                    break
        print(json.dumps({"turn": text, "first_text_s": round(first, 3) if first else None,
                          "total_s": round(time.monotonic() - t0, 3),
                          "reply": "".join(parts).strip()[:400]}))
    end = time.monotonic() + a.wait_push
    while time.monotonic() < end:
        try:
            async with asyncio.timeout(max(0.1, end - time.monotonic())):
                raw = await ws.recv()
        except TimeoutError:
            break
        if isinstance(raw, bytes):
            continue
        ev = json.loads(raw)
        if ev.get("type") in ("assistant_done", "followup", "background"):
            print(json.dumps({"later": ev.get("type"),
                              "text": str(ev.get("text") or ev.get("result") or "")[:400]}))
    await ws.close()


if __name__ == "__main__":
    asyncio.run(main())
