"""A stand-in for the worker-run agents-claude-code 0.5.1 (its manifest is the
real one, from before it moved to the hub): an older worker keeps announcing
it after the hub is upgraded. A turn here would say so."""


async def turn(ctx, prompt, **args):
    return {"text": "the old worker-run plugin answered", "session": "old"}
