def say(ctx, text):
    return text


def greet(ctx, name="world"):
    return f"{ctx.settings['greeting']} {name}"


def secret(ctx):
    return ctx.settings["token"]


def motto(ctx):
    return ctx.settings["motto"]


def on(ctx):
    return "on"
