# House agent memory: prototype (not for merge)

A standalone prototype of the house agent's memory (briefs MEM1 to MEM4). Not wired into Rookie,
the hub, any plugin or the beta. Findings: round one [REPORT.md](REPORT.md) (its code is in git at
b5c364a3), round two [REPORT2.md](REPORT2.md), round three [REPORT3.md](REPORT3.md), round four
[REPORT4.md](REPORT4.md).

Round four: the dream writes a graph (things, their aliases, and edges: about, opposes, refines,
part of, related to), and recall reads it with Python's sqlite3 alone (`search.model = "graph"`, the
default now): no model, no embeddings. Live turns log possible misses; the next dream heals real ones
with an alias in the words that missed. After Bake's answers at 16:17: the agent decides what is
definitive, every pin is said in the reply and can be undone at any time; another agent's or the
steward's correction never flips a belief (the house agent asks Bake once); and a narrower or
for-now wish sits beside a standing one (exception and for-now edges) instead of counting against
it. `python3 -m rook_memory misses` lists misses and heals.

Round three added: strong evidence flips and weak does not (`flip_at`); a definitive statement pins
and the pin keeps his words; how (the steward and agents decide) versus should (only Bake); one
house session that dreams and compacts between days; opposites behind a knob; a same/opposite check
before any new belief; and six searches to choose from (`search.model`: bge-small, bge-base, the
static tables blink-8M and potion-base-8M, keyword over SQLite full-text with the dream's other
wordings, and hybrid).

## Shape

- **The record and the scratch pad.** Every message is recorded word for word, stamped with when
  and who said it (bake, steward, agent, other) and the channel (chat, voice). The part no dream has
  covered yet is the scratch pad.
- **Episodes.** Facts a dream distilled, each tied to the entries it came from and to who said it
  (taken from the record, not from the model).
- **Beliefs.** Lasting statements. A belief has no score of its own: each **link** to an episode says
  whether that episode supports it or counts against it, and how strongly (kind x who x channel). The
  standing is worked out from the links: the most recent link sets the direction, and maturity (the
  separate occasions behind that direction) sets the certainty. A fresh one-off against a mature
  belief shows as "ask before acting". Nothing is retired or replaced.
- **Pins.** Set only from Bake's own words in a live turn (the `pin` tool), lifted the same way
  (`unpin`). A pinned belief stands whatever dreams link to it; the links still show.
- **Two clocks.** When something happened never changes. When it was last used moves only when a
  real turn with Bake pulls it up. A dream reading memory is not use.
- **Live turns.** Each live session is a real Claude Code session (`claude -p`, then `--resume`).
  Every turn, recall runs on the incoming message and the closest memories, with ids, go in front of
  the agent; it has `search`, `pin` and `unpin` tools (a small MCP server in this package).
- **The dream.** One more turn of the session: a fork (`claude -p --resume ID --fork-session`, saving
  nothing) with the memory search tool. It distils facts, then compares each with what is held
  (supports, against, or new). It runs when a session has been idle (a knob), at compaction (the
  `hook` command, for Claude Code's PreCompact hook), or by hand. Conversations taken in from an
  export have no live session; they are replayed in one fresh call instead.
- **Search.** By meaning: BAAI/bge-small-en-v1.5, run on this machine in numpy (fetched once into
  `<store>/models`). A search that clears nothing is "I don't remember", and nothing is written down.

## Run it

Python 3.11 or newer with numpy, and Claude Code signed in. cachyrig's system numpy uses reference
BLAS and embeds about eleven times slower, so use a numpy wheel: `uv venv ~/.rook-memory-proto/venv
&& VIRTUAL_ENV=~/.rook-memory-proto/venv uv pip install numpy`, then run with that venv's `python`
(written `python3` below). From this folder:

```sh
python3 -m rook_memory play madeup/conversations.json   # live turns + a dream after each conversation
python3 -m rook_memory check                            # the made-up set's expectations
python3 -m rook_memory ask "What wake word am I using?" # what recall would hand a turn, and why
python3 -m rook_memory history b3                       # one belief's whole history
python3 -m rook_memory show [sessions|scratch|record|episodes|beliefs|dreams|calls|tools]
python3 -m rook_memory say house "Is Starscream back?"  # one live turn (session "house")
python3 -m rook_memory pin "Starscream is gone for good" ; python3 -m rook_memory unpin
python3 -m rook_memory take EXPORT.zip --list           # a Claude data export
python3 -m rook_memory take EXPORT.zip --pick NAME --dream
python3 -m rook_memory dream [SESSION] [--all]
python3 -m rook_memory cases                            # what flips, and the score cases, with these knobs
python3 -m rook_memory repeat 5 --into DIR [--replay]   # five plays at once, then how often each check failed
python3 -m rook_memory tally DIR/run-*                  # the same tally for stores already played
python3 -m rook_memory compare-models bge-small blink-8M keyword   # misses, noise, time on the recall questions
python3 -m rook_memory opposites                        # pairs marked as opposites, knob off and on
python3 -m rook_memory bench blink-8M                   # load, search and embed times, peak memory
python3 -m rook_memory bench graph                      # the graph read path (sqlite3 only)
python3 -m rook_memory neighbourhood Starscream         # a thing's or belief's (t3, b12) neighbourhood
python3 -m rook_memory misses                           # possible misses and how dreams healed them
python3 -m rook_memory heal-test --others bge-small blink-8M keyword   # reworded questions: cold, live, healed
tools/vm-bench.sh PYTHON > results.jsonl                # the same under the Oracle free-VM shapes (systemd scopes)
python3 -m rook_memory reset
```

`--store DIR` (default `~/.rook-memory-proto`), `--knobs FILE` and `--set section.key=value` go
before the command.

For compaction, a PreCompact hook in the house agent's Claude Code settings would run
`python3 -m rook_memory hook` (it reads the hook's JSON on stdin and dreams that session). Nothing
here installs it.

## Knobs

Every design knob is in [knobs.toml](knobs.toml), with a comment each: models, who is who, the
import, when dreams run, the link strengths and maturity, search, and every prompt. None of them
refuse or stop anything.

## Tests

The plain mechanics (standing, pins, who-said-it rules, recall and the clocks, the importer, the MCP
tools, the command line), with stand-ins for the model and the embedder:

```sh
uv run --no-project --with pytest --with numpy python -m pytest proto/memory/tests   # from the repo root
```

What a model does with the rules is tested by the made-up set: `play` it into an empty store, then
`check`.
