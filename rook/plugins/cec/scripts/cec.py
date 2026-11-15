"""cec: the Pico-CEC bridge's text protocol, one command line and one reply
line per connection, over a TCP stream from the appliance's delegate."""

from rook.kit.net import Stream, values


async def _ask(ctx, line: str, ok: str) -> str:
    a = values(ctx.appliance)
    try:
        async with await Stream.tcp(ctx, ctx.worker["id"], a["host"], a["port"]) as s:
            await s.send(line)
            reply = (await s.read_until(b"\n", timeout=4)).decode("utf-8", "replace").rstrip("\r\n")
    except ctx.Error as e:
        raise ctx.Error(f"failed: {e.message}") from None
    if not reply.startswith(ok):      # ERR ..., or something else
        raise ctx.Error(f"failed: {reply[:500]}")
    return reply


async def send(ctx, addr, opcode, operands=()):
    return await _ask(ctx, " ".join(["send", addr, opcode, *operands]) + "\n", "OK")


async def status(ctx):
    return await _ask(ctx, "ping\n", "pong")
