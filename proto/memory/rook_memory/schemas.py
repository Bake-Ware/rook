"""The shape of the dream's answer, which Claude Code holds the model to (`claude -p --json-schema`)."""


def dream(knobs):
    abouts = [knobs["people"]["person"], "world", *knobs["people"]["others"]]
    return {
        "type": "object",
        "properties": {"facts": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "fact": {"type": "string"},
                "entries": {"type": "array", "items": {"type": "string"}},
                "said_by": {"type": "string", "enum": [knobs["people"]["person"], *knobs["people"]["others"]]},
                "kind": {"type": "string", "enum": ["correction", "statement", "mention", "unclear"]},
                "held": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"belief": {"type": "string"},
                                   "stance": {"type": "string", "enum": ["supports", "against"]},
                                   "why": {"type": "string"}},
                    "required": ["belief", "stance"]}},
                "new_belief": {"anyOf": [{"type": "null"}, {
                    "type": "object",
                    "properties": {"belief": {"type": "string"}, "about": {"type": "string", "enum": abouts}},
                    "required": ["belief", "about"]}]},
            },
            "required": ["fact", "entries", "said_by", "kind", "held", "new_belief"]}}},
        "required": ["facts"],
    }
