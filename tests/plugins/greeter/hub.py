"""The sample plugin `greeter`, its hub part: cap `greet.hub`. Its worker part is worker.py."""


def setup(plugin):
    @plugin.cap("greet.hub", "Say hello from the hub.", {
        "type": "object", "additionalProperties": False, "properties": {"name": {"type": "string"}}})
    def hello(call, args):
        return {"greeting": f"Hello from the hub, {args.get('name', 'you')}."}
