"""Errors a config action raises. Messages are already redacted."""


class ActionError(Exception):
    """A clean failure: the cap fails with this message. ``retry`` says a
    retry might succeed (timeouts, refused connections)."""

    def __init__(self, message: str, *, retry: bool = False):
        super().__init__(message)
        self.retry = retry


class Unavailable(ActionError):
    """The action can't run on this worker: an action type it doesn't know,
    or none of the binaries it names is installed."""


class Secret(str):
    """A value from the vault (a secret setting or appliance setting). Works
    as a plain string; templating remembers it so errors never show it."""
