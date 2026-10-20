---
id: names
title: Record names you can say
order: 8
features:
  - features/record-names
since: 0.1.0
deprecated: false
---
# Record names you can say

Every console room, handoff thread, task, project, job, job run, huddle,
huddle card and chat room has a short name made of three everyday words,
such as `brave-otter-lamp`. The name is there so you can say it to a voice
assistant, read it out to someone, or type it on a phone, instead of a long
code like `3f2a9c1d00aa11bb`.

Knowledge pages keep their own page names (slugs), and releases keep their
release names.

## Where you see names

- On the dashboard the name is the main label of a task, project, job, run,
  huddle, chat room, console room and handoff. The record's id is still
  there: open the item's details, or tap the id to copy it.
- Agents' replies give both the name and the id.
- Voice answers, handoffs and notifications use the name.

## Using a name

Anywhere you can give an id, you can give the name instead: in the
dashboard's search boxes and links, to an agent, by voice, or in `rook`
commands. Case and separators don't matter, so all of these mean the same
record:

```
brave-otter-lamp
Brave Otter Lamp
brave_otter_lamp
```

Search finds records by name too, including part of a name.

## When a name is misheard

If a name doesn't match exactly, Rook looks for close ones (one word
misheard, or a small typo):

- **Looking something up** (reading a task, a room, a job's runs) uses the
  one close match if there is exactly one, and says it did.
- **Changing or deleting something** (posting, closing, cancelling, editing,
  approving a card) never guesses. You get the close names back as
  suggestions and say the exact one, or use the id.
- If several records are close, you get the list and pick one.

## How names are made

Names are an adjective followed by two nouns, drawn from a list of about two
thousand short words chosen to be easy to say and hear. The list has no
words that sound alike (no "sea" and "see"), nothing rude, violent or sexual,
no brand names, and no words that sound like a command ("stop", "delete",
"cancel", "yes", "no").

- A name is unique among records of the same kind on a band: two tasks
  never share a name. A task and a job may, rarely.
- A name never changes once it is given.
- Records that existed before names arrived were given one when the hub was
  upgraded.

## For administrators

Names live in a `record_names` table inside each store's database, beside
the records they name. The hub creates the table and names any unnamed
records each time it starts, so an upgrade needs no separate step. The
record ids are unchanged; dropping the table removes the names and nothing
else, and an older release simply ignores it.
