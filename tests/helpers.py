import asyncio

from nacl.public import PrivateKey

from rook_protocol import sign_link_key


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


def link_fields(signing_key):
    """A knock's link_key and link_signature for a throwaway identity: a fresh X25519 key,
    signed by `signing_key` (docs/protocol.md, knock)."""
    link_key = PrivateKey.generate().public_key.encode().hex()
    return {"link_key": link_key, "link_signature": sign_link_key(signing_key, link_key)}
