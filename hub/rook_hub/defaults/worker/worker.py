"""worker: whether this worker answers, shipped as a plugin installed by default (PL7).
docs/inventory.md CAP-05. worker.ping is 0.5's (origin/castling rook/core2/builtin/worker.yaml
and rook/core2/worker/builtins.py), by Bake's answer of 17 November: no arguments, and
{pong: true, at}, `at` being the worker's clock in seconds since 1970."""

import time

from rook_worker import Cap


async def ping(args):
    extra = sorted(args)
    if extra:
        raise ValueError(f"worker.ping takes no argument {', '.join(extra)}; it takes none")
    return {"pong": True, "at": time.time()}


CAPS = {
    "worker.ping": Cap(
        "Whether this worker answers: gives pong true and at, the worker's clock in seconds since 1970.",
        {"type": "object", "additionalProperties": False, "properties": {}},
        ping),
}
