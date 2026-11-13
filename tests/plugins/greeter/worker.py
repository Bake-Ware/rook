"""The sample plugin `greeter`, its worker part for Python workers: a cap that returns a
result, `greet.hello`, and one that opens a stream, `greet.echo` (docs/plugins.md)."""

import asyncio

from rook_worker import Cap

GREETING = "Hello"
serving = set()     # the echo tasks, kept so they are not collected while they run


async def hello(args):
    return {"greeting": f"{GREETING}, {args.get('name', 'you')}."}


async def echo(args, stream):
    async def answer():
        while (data := await stream.read()) is not None:
            await stream.send(b"greeter heard: " + data)
    task = asyncio.create_task(answer())
    serving.add(task)
    task.add_done_callback(serving.discard)
    return {"title": args.get("title", "greeter echo"), "description": "Echoes what is written to it."}


CAPS = {
    "greet.hello": Cap("Say hello.", {"type": "object", "properties": {"name": {"type": "string"}}}, hello),
    "greet.echo": Cap("Open a stream that echoes what is written to it.",
                      {"type": "object", "properties": {"title": {"type": "string"}}}, echo, opens_stream=True),
}
