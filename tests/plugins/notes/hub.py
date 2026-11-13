"""The sample plugin `notes`: it owns record type `note` and adds caps
`note.create` and `note.search`; creating a note emits `note.created`."""


def setup(plugin):
    plugin.record_type("note")

    @plugin.cap("note.create", "Make a note with a title and some text.", {
        "type": "object", "required": ["title", "text"], "additionalProperties": False,
        "properties": {"title": {"type": "string"}, "text": {"type": "string"}}})
    def create(call, args):
        note = call.create("note", args["title"], body={"text": args["text"]})
        call.emit("note.created", {"note": note["guid"], "note_moniker": note["moniker"], "title": note["title"]})
        return {"note": note["moniker"], "guid": note["guid"]}

    @plugin.cap("note.search", "Find the notes that hold every word of text.", {
        "type": "object", "additionalProperties": False, "properties": {"text": {"type": "string"}}})
    def search(call, args):
        return {"notes": [{"note": n["moniker"], "title": n["title"], "text": n["body"]["text"]}
                          for n in call.search("note", args.get("text"))]}
