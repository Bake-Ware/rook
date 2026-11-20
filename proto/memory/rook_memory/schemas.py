"""The JSON shapes the model answers in, for `claude -p --json-schema` (knob model.structured_output)."""

EPISODES = {"type": "object", "required": ["episodes"], "properties": {"episodes": {"type": "array", "items": {
    "type": "object", "required": ["what", "importance", "first_line", "last_line"], "properties": {
        "what": {"type": "string"}, "importance": {"type": "integer", "minimum": 1, "maximum": 10},
        "first_line": {"type": "integer"}, "last_line": {"type": "integer"}}}}}}

QUESTIONS = {"type": "object", "required": ["questions"],
             "properties": {"questions": {"type": "array", "items": {"type": "string"}}}}

BELIEFS = {"type": "object", "required": ["beliefs"], "properties": {"beliefs": {"type": "array", "items": {
    "type": "object", "required": ["belief", "because", "confidence", "importance", "supersedes", "how"], "properties": {
        "belief": {"type": "string"}, "because": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "importance": {"type": "integer", "minimum": 1, "maximum": 10},
        "supersedes": {"type": ["string", "null"]},
        "how": {"type": ["string", "null"], "enum": ["contradicts", "refines", None]}}}}}}
