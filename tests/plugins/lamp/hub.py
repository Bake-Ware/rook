"""The sample driver `lamp`: a lamp with a small HTTP API (the test starts one).

Caps lamp.on and lamp.off, and a status function that reads the lamp's state.
It sends the key from the device's config as a header; the config holds it as
secret:<name>, and the hub resolves it before the driver sees it."""

import json


def setup(plugin):
    lamp = plugin.driver("lamp")

    async def ask(device, path, method):
        answer = await device.http(device.config["url"] + path, method=method,
                                   headers={"x-lamp-key": device.config.get("key", "")}, wait=5)
        return json.loads(answer["body"])

    @lamp.cap("lamp.on", "Turn the lamp on.", {"type": "object", "additionalProperties": False, "properties": {}})
    async def on(device, args):
        return await ask(device, "/on", "POST")

    @lamp.cap("lamp.off", "Turn the lamp off.", {"type": "object", "additionalProperties": False, "properties": {}})
    async def off(device, args):
        return await ask(device, "/off", "POST")

    @lamp.status
    async def status(device):
        return await ask(device, "/state", "GET")
