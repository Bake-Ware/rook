# Memory prototype for the house agent (MEM1)

A prototype to learn from and iterate on with Bake, not something to merge. It is not wired into
Rookie, the hub, any plugin or the beta. It reads closed Claude Code sessions and builds:

- **a scratch pad**: everything said and done in a session, word for word (tool output cut short),
  kept until a dream pass condenses it;
- **episodes**: plain-language records of what happened, each with when, when last read, an
  importance from 1 to 10 given by the model, and the session file and lines it came from;
- **beliefs**: lasting statements made by a dream pass, each with a confidence, the episodes and
  beliefs that support it, and, when it contradicts or refines an older belief, a link to it. The
  old belief is kept and marked superseded, never deleted.

What we found on real sessions is in `REPORT.md`.

## Running it

Python 3.11 or newer, no packages beyond the standard library, and Claude Code (`claude`) signed in
on the machine. From this folder:

```sh
python -m rook_memory take ~/.claude/projects/<folder>/<session>.jsonl ...   # scratch pad + episodes; dreams by itself at the threshold
python -m rook_memory dream                                                  # a dream pass now
python -m rook_memory ask "What does Bake want from the voice app?"          # what a turn would be given, with scores
python -m rook_memory show [sessions|episodes|beliefs|scratch|dreams|calls]  # everything, readable
python -m rook_memory reset                                                  # remove the store
```

The store is one sqlite file, `~/.rook-memory-proto/memory.sqlite3` unless `--store DIR` says
otherwise. `--knobs FILE` uses another knobs file.

## Turning the knobs

Every knob is in `knobs.toml`, with a comment each: the models, how sessions are cut up, the dream
threshold and sizes, the recency decay, the three weights, how many items a turn gets, and every
prompt. A round of iteration is an edit to that file, then `reset` and `take` again (or `ask` again,
for the ranking knobs, which need no model call).

## How it works

- `take` reads a session file into entries (`rook_memory/transcript.py`), puts them in the scratch
  pad, and reads them to the model in stretches of `chunk_chars`; the model writes episodes with an
  importance and a line range. When the importance of the episodes not yet dreamed over adds up to
  `when_importance_sum_reaches`, a dream pass follows.
- `dream` (`rook_memory/dream.py`) asks the model for the most salient questions about the new
  episodes; for each, retrieves memories as a turn would and asks for beliefs with confidence,
  support and what they supersede. Then it condenses the scratch pad of all but the most recent
  sessions.
- `ask` (`rook_memory/recall.py`) scores every episode and standing belief, and separately every
  scratch entry: recency (decay per hour since last read) + importance + relevance (BM25 over
  words), each scaled to 0..1 across the candidates and weighted. It prints the top ones with every
  part of their score and the words that matched.
- Every model call is `claude -p` with no tools, settings, MCP servers or saved session, in an
  empty folder (`rook_memory/model.py`), and is recorded with its time and cost (`show calls`).

## Tests

The plain mechanics (store, scoring, supersedence, condensing, the dream's wiring with a stand-in
model), not model output. From the repository root:

```sh
.venv/bin/python -m pytest proto/memory/tests
```
