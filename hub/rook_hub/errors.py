"""Errors the hub hands back. Every one says what to try next (C3)."""


def error(code, message, next_step, call_id=None):
    """An `error` message (C3: it always says what to try next)."""
    reply = {"type": "error", "code": code, "message": message, "next": next_step}
    if call_id is not None:
        reply["id"] = call_id
    return reply


class HubError(Exception):
    """A hub function could not do what it was asked. `.message` is an `error` message."""

    def __init__(self, code, message, next_step):
        super().__init__(message)
        self.message = error(code, message, next_step)
