"""net: the dumb network primitives every worker serves (D3), shipped as a plugin
installed by default (PL7): net.http, net.tcp and net.udp.

The functions are rook_net's, which the hub also runs itself for a device on a
direct route (D4), so both answer alike. Each blocks, so a call runs it in a thread."""

import asyncio

import rook_net
from rook_worker import Cap


def _serve(function):
    async def handler(args):
        return await asyncio.to_thread(function, args)
    return handler


CAPS = {name: Cap(description, schema, _serve(function))
        for name, (function, description, schema) in rook_net.CAPS.items()}
