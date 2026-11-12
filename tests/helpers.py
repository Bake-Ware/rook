import asyncio


class Clock:
    """A clock the test moves by hand, so H3 needs no real waiting."""

    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


async def eventually(check, timeout=5.0):
    """Wait until check() returns something truthy, and return it."""
    async with asyncio.timeout(timeout):
        while True:
            value = check()
            if value:
                return value
            await asyncio.sleep(0.01)
