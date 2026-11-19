"""The stress test's own plugin (round 8, tests/stress/size.py), installed only on the round's
throwaway hub: records exist only through plugin code (K2), so this is how the round makes 50,000
of them and queries them. It owns record type `stress`."""

WORDS = ["amber", "basil", "cedar", "delta", "ember", "fjord", "garnet", "harbor", "indigo", "juniper"]


def setup(plugin):
    plugin.record_type("stress")

    @plugin.cap("stress.fill", "Make n stress records, numbered from start.", {
        "type": "object", "required": ["start", "n"], "additionalProperties": False,
        "properties": {"start": {"type": "integer"}, "n": {"type": "integer"}}})
    def fill(call, args):
        for i in range(args["start"], args["start"] + args["n"]):
            call.create("stress", f"stress record {i}", f"{WORDS[i % 10]} {WORDS[i // 10 % 10]} number{i}",
                        body={"n": i, "text": f"{WORDS[i // 100 % 10]} body of record {i}"})
        return {"made": args["n"]}

    @plugin.cap("stress.search", "Find stress records holding every word of text, at most limit.", {
        "type": "object", "additionalProperties": False,
        "properties": {"text": {"type": "string"}, "limit": {"type": "integer"}}})
    def search(call, args):
        found = call.search("stress", args.get("text"), limit=args.get("limit", 50))
        return {"count": len(found), "first": found[0]["title"] if found else None}
