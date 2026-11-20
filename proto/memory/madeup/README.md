# The made-up test set (round two)

Everything in `conversations.json` is **made up**: invented for this prototype, not anything Bake said.
Each conversation's name starts with "MADE UP" and carries `"made_up": true`. It has the shape of a
Claude data export, plus two fields per message the real export lacks: `speaker` (bake, steward,
agent, other, or house for the house agent's own lines) and `channel` (chat or voice).

What each one exercises:

| # | date | what it tests |
|---|------|---------------|
| 01 | 4 May | the wake word in use; Starscream correction 1 (a backup agent plans on it) |
| 02 | 19 May | dark mode (start of the flip); Starscream correction 2; wake word again |
| 03 | 2 Jun | the steward: its own wish (digest at 7), Bake quoted second-hand (PDFs), its decision (weekly log rotation); a log agent's decision |
| 04 | 10 Jun | a garbled voice line that must not become confident beliefs |
| 05 | 16 Jun | Starscream correction 3, then said outright and forcefully: pin; a mistaken pin undone in the next message |
| 06 | 20 Jun | the wake word a third time (it is mature now) |
| 07 | 1 Jul | "We're dropping wake word": one statement against a mature belief |
| 08 | 8 Jul | dashboard to light mode (the flip); no wake word, second occasion |
| 09 | 20 Jul | dashboard back to dark (the flip back); no wake word, third occasion |
| 10 | 5 Aug | an agent claims Starscream is back (against a pinned belief) |
| 11 | 12 Aug | questions: where a backup goes, the wake word, something never said, the steward's PDF claim |

`play` sends every line except the house agent's own to a live house-agent session (the agent answers
for itself) and dreams at the end of each conversation. `take` records the lines as written, house
lines included, and `take --dream` replays each. `expect.toml` is what should hold afterwards
(`python -m rook_memory check`).
