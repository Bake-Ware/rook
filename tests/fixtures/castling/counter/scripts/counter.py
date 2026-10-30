import asyncio
import sys
import time

VERSION = 1
STATE = {"count": 0, "started": False, "events": []}


def start(ctx):
    STATE["started"] = True
    STATE["events"].append("start")


def stop(ctx):
    STATE["started"] = False
    STATE["events"].append("stop")


def status(ctx):
    return {"count": STATE["count"], "version": VERSION}


def warm(ctx, event, payload):
    STATE["events"].append(("warm", event, payload))


def add(ctx, n):
    STATE["count"] += n * ctx.settings["step"]
    return STATE["count"]


def version(ctx):
    return VERSION


def whoami(ctx):
    return {"person": ctx.person, "agent": ctx.agent, "why": ctx.why}


def file(ctx):
    return ctx.file(data=b"hi", name="a.txt")


def crash(ctx):
    raise RuntimeError("kaboom")


def quit(ctx):
    sys.exit(3)


def refuse(ctx):
    raise ctx.Error("device busy", retry=True)


async def slow(ctx):
    try:
        await asyncio.sleep(30)
    except asyncio.CancelledError:
        STATE["events"].append("slow-cancelled")
        raise


def hold(ctx):
    STATE["events"].append("hold-start")
    time.sleep(0.6)
    STATE["events"].append("hold-end")


def poke(ctx):
    STATE["events"].append("poke")
    return "poked"
