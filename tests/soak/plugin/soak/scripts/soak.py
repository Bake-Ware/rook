import asyncio
from pathlib import Path

import yaml

VERSION = yaml.safe_load((Path(__file__).parent.parent / "plugin.yaml").read_text())["version"]


def ping(ctx, text=""):
    return {"text": text, "person": ctx.caller.person, "agent": ctx.caller.agent, "version": VERSION}


def blob(ctx, size=0):
    return {"size": size, "data": "x" * size}


async def sleep(ctx, seconds=0.0):
    await asyncio.sleep(seconds)
    return {"slept": seconds}


def wipe(ctx):
    return {"wiped": False}
