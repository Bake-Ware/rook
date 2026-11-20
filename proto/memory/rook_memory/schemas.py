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
                                   "other_wordings": {"type": "array", "items": {"type": "string"}},
                                   "about_things": {"type": "array", "items": {"type": "string"}},
                                   "refines": {"type": ["string", "null"]},
                                   "exception_to": {"type": ["string", "null"]},
                                   "for_now_beside": {"type": ["string", "null"]}},
                    "required": ["belief", "about"]}]},
            },
            "required": ["fact", "entries", "said_by", "kind", "held", "new_belief"]}},
            "things": {"type": "array", "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "id": {"type": ["string", "null"]},
                               "aliases": {"type": "array", "items": {"type": "string"}},
                               "entries": {"type": "array", "items": {"type": "string"}}},
                "required": ["name", "aliases"]}},
            "about": {"type": "array", "items": {
                "type": "object", "properties": {"belief": {"type": "string"}, "thing": {"type": "string"}},
                "required": ["belief", "thing"]}},
            "relations": {"type": "array", "items": {
                "type": "object",
                "properties": {"thing": {"type": "string"}, "rel": {"type": "string", "enum": ["part_of", "related"]},
                               "other": {"type": "string"}, "entries": {"type": "array", "items": {"type": "string"}}},
                "required": ["thing", "rel", "other"]}},
            "heals": {"type": "array", "items": {
                "type": "object",
                "properties": {"miss": {"type": "string"}, "thing": {"type": "string"}, "alias": {"type": "string"},
                               "belief": {"type": ["string", "null"]}, "why": {"type": "string"}},
                "required": ["miss", "thing", "alias"]}},
            "nothing_held": {"type": "array", "items": {"type": "string"}},
            "settle": {"type": "array", "items": {
                "type": "object", "properties": {"belief": {"type": "string"},
                                                 "how": {"type": "string", "enum": ["lasting", "lapsed"]}},
                "required": ["belief", "how"]}},
            "changes": {"type": "array", "items": {
                "type": "object",
                "properties": {"belief": {"type": "string"}, "now": {"type": "string"},
                               "entries": {"type": "array", "items": {"type": "string"}}, "why": {"type": "string"}},
                "required": ["belief", "now", "entries"]}}},
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
