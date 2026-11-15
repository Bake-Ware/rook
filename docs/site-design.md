# The site: a house the agent keeps

Bake's site design, as decided with him on 2026-11-10. With `docs/castling-design.md` this is the standard the site's code is reviewed against. It is kept as he gave it; change it only when he does.

The site is a house the agent keeps, not a product with one fixed layout. Three layers: what exists (every page and setting a plugin provides, each with a default home), the layout (what shows, where, under what name, per person), and the agent, who owns the layout and tailors it to whoever is looking.

## 1. Two panes everywhere

Two panes everywhere. The first-run workflow is the normal one: the work on the left, the agent chat on the right, pointing at controls, opening sections, making changes and explaining. Clicking and talking reach the same places. Bake: it "creates a focus on the activity and not the conversation". The person can put the chat pane on the left or the right (per-person setting; the agent can flip it when asked). The chat can POP OUT into its own browser window: same chat, same stream; closing the window docks it back; Rookie leaves one window and arrives in the other (the existing enter/exit animation), since a browser cannot draw across windows.

## 2. The sidebar

Sidebar: a table of contents, organized and alive, not a short menu. At the top, Pinned: a per-person strip, always open; a person pins a page, or the agent pins one it notices they use. Below it four segments to start: Agents, Machines, Work, Hub. Frequent segments open, the rest folded; folded segments show what exists without shouting. A plugin's page goes into a segment (its manifest names which), never a new top-level entry; a plugin may add a segment. The agent can move, hide, rename, group, add a page and publish a page, per person; it never deletes a plugin's page, only hides it. Agent-made pages live as records and one can be published at a public address.

## 3. Settings

Settings are the map of what the system can do: people read them to learn its shape, even when they change most things by asking the agent. Every setting carries a one-line description. Two tiers: (a) real pages for what people want hands-on control of: agents (each agent, its engine, its model, persona; the attention pass on/off), model providers, engines, voice, under the Agents segment; (b) ONE generated Settings page for everything else, built from manifests, grouped by segment, with real controls (a dropdown where the values are known, not a text box). A plugin's manifest says which segment its settings belong to, one line; it never adds a page for them. Every setting is also changeable by a cap, so the agent can change any of it.

## 4. Out of the box

Out of the box: nothing is curated out; the levels decide what starts switched on.

## Also from the same brief

Keep the Rook look (DM Sans / IBM Plex, the 3D rook, Rookie). Slim: no pages beyond what the design names.
