"""The shape of the dream's answer, which Claude Code holds the model to (`claude -p --json-schema`)."""


def dream(knobs):
    abouts = [knobs["people"]["person"], "how", "world"]
    return {
        "type": "object",
        "properties": {"facts": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "fact": {"type": "string"},
                "entries": {"type": "array", "items": {"type": "string"}},
                "said_by": {"type": "string", "enum": [knobs["people"]["person"], *knobs["people"]["others"]]},
                "kind": {"type": "string", "enum": ["correction", "decision", "statement", "mention", "unclear"]},
                "held": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"belief": {"type": "string"},
                                   "stance": {"type": "string", "enum": ["supports", "against"]},
                                   "why": {"type": "string"}},
                    "required": ["belief", "stance"]}},
                "new_belief": {"anyOf": [{"type": "null"}, {
                    "type": "object",
                    "properties": {"belief": {"type": "string"}, "about": {"type": "string", "enum": abouts},
                                   "opposite_of": {"type": ["string", "null"]},
                                   "other_wordings": {"type": "array", "items": {"type": "string"}}},
                    "required": ["belief", "about"]}]},
            },
            "required": ["fact", "entries", "said_by", "kind", "held", "new_belief"]}}},
        "required": ["facts"],
    }


SAME = {
    "type": "object",
    "properties": {"verdicts": {"type": "array", "items": {
        "type": "object",
        "properties": {"pair": {"type": "integer"}, "verdict": {"type": "string", "enum": ["same", "opposite", "different"]}},
        "required": ["pair", "verdict"]}}},
    "required": ["verdicts"],
}
