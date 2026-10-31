"""The fake house agent: "make a page <name>", "archive <page>", the
site's first-visit note, else an echo."""
import json


async def turn(ctx, prompt, session=None, cwd=None, model=None, effort=None, system=None, timeout=None):
    if system and "curate an agent's memory" in system:
        return {"text": json.dumps({"add": []}), "session": "dream"}
    said = prompt.strip().splitlines()[-1].strip()
    if "First visit to the hub site" in prompt:
        return {"text": "Welcome! How do you want to use the site?", "session": "s1"}
    words = said.split()
    if said.startswith("make a page ") and len(words) == 4:
        name = words[3]
        have = {c["component"] for c in (await ctx.call("pages.components"))["components"]}
        layout = [{"component": "hub/workers"},
                  {"component": "pages/text", "props": {"text": "Asked for in the dock."}, "title": "Why"}]
        if "task/list" in have:
            layout.append({"component": "task/list", "wide": True})
        made = await ctx.call("pages.create", name=name, title=name.title(), layout=layout,
                              note=f"The person asked for a {name} page in the site's dock.")
        await ctx.call("pages.start", page=made["page"])
        return {"text": f"Made {made['page']} (task {made.get('task')}) and made it your start page.",
                "session": "s1"}
    if said.startswith("archive ") and len(words) == 2:
        await ctx.call("pages.archive", page=words[1], reason="The person is done with it.")
        return {"text": f"Archived {words[1]}; restore brings it back.", "session": "s1"}
    return {"text": "brain: " + said, "session": session or "s1"}
