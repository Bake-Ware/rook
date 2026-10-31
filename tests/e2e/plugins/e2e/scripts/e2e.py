import os


def ping(ctx, text=""):
    return {"text": text, "person": ctx.caller.person, "agent": ctx.caller.agent, "pid": os.getpid()}


def admin(ctx):
    return {"changed": False}


def wipe(ctx):
    return {"wiped": True}


def receive(ctx, **text):
    ctx.emit("e2e.sms.received", text)
    return {"emitted": True}
