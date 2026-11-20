# Memory prototype, round four: a graph the dream writes, read with no model

Round four builds brief MEM4. All the thinking happens when memory is written, in the dream, on the
engine's machine. Reading is a lookup the hub can do with Python's sqlite3 alone: no model, no
embeddings. A miss is acceptable as long as it heals ("I think not remembering is fine so long as it
self heals").

Everything ran on the **made-up** conversations in [`madeup/`](madeup/), since his export has still
not arrived. Rounds one to three's reports are unchanged.

## What was built

- **Things.** What beliefs are about: Starscream, the wake word, backups, cachyrig, the dashboard,
  the morning digest. Each has a name and **aliases**, the other ways it is said ("the old server",
  "always listening", "photo backup"). Each alias is tied to the entries and the dream it came from.
- **Edges, written only by the dream and by `pin`:**
  - a belief is **about** a thing;
  - a belief **opposes** another;
  - a belief **refines** another ("light mode on the kitchen tablet for a while" refines "dark mode
    on everything", which round three wrongly read as its opposite);
  - a thing is **part of** or **related to** another.

  Every edge records the dream that wrote it and the entries it came from. Beliefs still **rest on**
  episodes through round two's links, unchanged; trueness still lives there.
- **Reading, with SQLite alone** (`search.model = "graph"`, now the default; the rounds-three
  searches are still a knob away):
  1. The message's words are matched against thing names and aliases with SQLite full-text search
     (FTS5, Porter stemming). A thing is hit when every content word of one of its names is in the
     message.
  2. From the things hit, recall walks out `hops` steps (default 1) and collects beliefs. Nearer is
     closer (`walk_decay` 0.6 a step).
  3. Beliefs, episodes and scratch entries whose own words (and the dream's other wordings) cover
     half the message are found directly.
  4. Opposites of what was found come along, marked "the opposite of bN".
  5. Ranking is by closeness, recency and certainty, as before; pins clear a lower floor; a miss is
     "I don't remember".

  A test checks that the read path never imports numpy.
- **Self-healing:**
  - Every live turn's recall is logged. A turn whose recall found no belief, or whose own `search`
    then found beliefs that recall had not given, is logged as a possible miss.
  - The next dream gets the list. For a real miss (a belief already held was what the message was
    after), it adds an alias in the message's own words, plus the about edge if it was missing, so
    the same wording hits next time.
  - Anything else is marked "nothing held".
  - Code refuses a "heal" for something that was not held when the message came: new information is
    not a miss.
  - Nothing is written about the gap itself.
- **Each thing once.** The dream has `things` and `neighbourhood` tools to look first. Code reuses a
  thing whenever one is already held by that name or alias, and only then makes a new one.
- **A fix for round three's over-pinning** (superseded by Bake's answer at 16:17; see the last
  section: the agent decides, every pin is said, any pin can be undone). `pin` needed `final_words`: the words in his message
  that make it final. Code checks they are there and contain one of a list in `knobs.toml` (final,
  never, always, for good, forever, permanent, not coming back, from now on, pin). Without them it is
  an ordinary correction, and nothing is pinned.
- **Commands:**
  - `neighbourhood t3|b12|NAME`: a thing's or belief's names, aliases, edges and evidence;
  - `misses`: every possible miss and how it was healed;
  - `heal-test`: reworded questions asked cold, then live with dreams, then cold again, against other
    searches too;
  - `bench graph`: the read path at 500, 5,000 and 50,000 beliefs, using sqlite3 only.
- A bug found on the way: `dream.run` passed only the facts of the dream's answer on. The first five
  plays lost their aliases, relations and heals, so they were set aside
  (`round4/lost-answers`) and run again.

## The numbers

### Misses and healing

There are 32 reworded questions ([`madeup/reworded.toml`](madeup/reworded.toml)), each about
something already held, in words the stored text does not use ("Is the kitchen speaker still always
listening?", "Is that old server we retired in the winter available again?", "When does my morning
briefing land?"). They were asked against a store from a full play (play run 1):
1. cold: recall only, no model;
2. live as Bake, eight a day: the house agent may search in its own words, and a dream follows each
   day;
3. cold again.

A question counts as found when recall turns up a belief, held before the test, with the right
words.

| | Found cold | Found after the dreams |
|---|---|---|
| **graph** | **18 of 32** | **24 of 32** (6 healed) |
| bge-small | 16 | 16 |
| blink-8M (static table) | 19 | 19 |
| keyword with the dream's rewordings | 16 | 16 |

Only the graph heals; the others find the same before and after.

What healed, and with which alias:

| Question | Healed with |
|---|---|
| "Do I still have to say the magic phrase before asking something?" | "the magic phrase", for the wake word |
| "When does my morning briefing land?" | "morning briefing", for the morning digest |
| "What tool maps the devices on the network each week?" | "maps the devices on the network each week", for the weekly sweep |
| "Are guests allowed on the same LAN as my own machines?" | "guests on the same LAN as my machines", for the guest network |
| "Which day is the weekly summary produced?" | "weekly summary produced", for the weekly report; the agent's own search had found it |
| "When does the nightly copy job kick off?" | "nightly copy job", for backups |

**What never healed, and why.** Eight questions stayed misses: the network storage box (twice; the
NAS is pinned), screen colours and the control panel at night (dark mode), the visitors' internet
code (guest wifi), documents in a printable format (the PDF claim), and the hall display (twice; the
dashboard). In every one, nobody found the answer:
- The house agent said "I don't remember" without searching, even when it named the right thing:
  "Do you mean a guest wifi setup? If so, I have nothing on it."
- The test does not have Bake answer.
- The dream, given the miss and the search tools, judged "nothing held".

Healing needs a finder: the agent's search, Bake supplying it, or the dream recognising it. When
none of the three does, the miss stays a miss. In real use Bake would answer ("the NAS, you know"),
and that is the heal; here he did not.

**Misses in ordinary plays.** Each play logged 17–19 possible misses. The dreams judged 14–17 of
them "nothing held" (new information: the first time something was said is not a miss) and healed
0 or 1. Most of what the log catches is not a miss at all.

### Against round three's searches

These are the same 32 questions on the same store before the test (Y = found):

- **Only the graph:** "Should you bother me about the garden watering?" (sprinklers, through the
  dream's aliases).
- **Graph and keyword but not the embeddings:** "What time does the daily summary arrive?" ("daily
  digest").
- **Graph and blink-8M but not keyword:** "How are the log files kept small?", "Can the backup go to
  the machine we shut down in February?".
- **Missed by the graph but found by keyword:**
  - "Should the control panel be bright or dim at night?"
  - "When does the nightly copy job kick off?"

  The graph's floor for a belief's own words is stricter (half the message's words, against
  keyword's third), a knob.
- **Only blink-8M (and bge-small):** "Is the network storage box still in service?", "What tool maps
  the devices...", "Which day is the weekly summary produced?". These are related meanings with no
  shared word and no alias yet. Two of them healed.
- **Missed by all four, cold:** the magic phrase, screen colours, morning briefing, visitors'
  internet code, printable format, hall display (twice).

### On the hub's budget

These are measured under systemd scopes, on **Python's own sqlite3 with numpy never imported**:
- the AMD micro as 1/8 of a core and 600 MB;
- the Arm shape as 2 cores and 12 GB, a stand-in on x86.

The stores are filler sentences, with N/5 things (two aliases each), 1.5 about-edges per belief and
N/10 thing-to-thing edges, plus the real Starscream so there is something to find. Starscream was
found at every size.

| Shape | Beliefs | Build the indexes (once) | One recall | Peak memory |
|---|---|---|---|---|
| micro | 500 | 0.19 s | 0.004 s | 29 MB |
| micro | 5,000 | 0.69 s | 0.30 s | 82 MB |
| micro | 50,000 | 8.2 s | 2.6 s | 82 MB |
| Arm stand-in | 500 | 0.02 s | 0.005 s | 82 MB* |
| Arm stand-in | 5,000 | 0.12 s | 0.04 s | 82 MB* |
| Arm stand-in | 50,000 | 1.3 s | 0.54 s | 82 MB* |

\* One process ran all three sizes, so the peak is the largest one's.

For comparison, round three under the same micro limits: bge-small 158 MB and 0.3 s a search at 500;
blink-8M 55 MB and 5 ms. The graph at 500 is **29 MB and 4 ms**. Python with sqlite3 alone is 12 MB.
At 50,000 the time is Python reading every row of every table for each recall, not the full-text
search, and it would come down by ranking in SQL first.

### Dream cost

| | Round three | Round four (writes the graph) |
|---|---|---|
| Forked dream (mean of 70) | $0.040, 4.2 model turns inside | $0.045, 4.0 model turns inside |
| Replayed dream (mean of 70) | $0.012 | $0.016 |
| One play, all calls | $1.60–1.83 | $1.69–1.83 |
| One replay | $0.16–0.21 | $0.20–0.31 |

Writing the graph adds about half a cent a dream. The answers are longer, and the same number of
lookups happens inside. Over the 70 dreams of the five plays, a dream wrote 0–6 new things (most
reused what was held), 0–20 aliases (median 5) and 0–14 edges (median 2).

### Duplicates over five runs

| | 5 plays | 5 replays |
|---|---|---|
| Things | 18–21 per run | 17–20 |
| Aliases | 67–83 | 59–65 |
| About edges / thing-to-thing / refines / opposes | 34–38 / 9–10 / 3–6 / 1–2 | 29–35 / 2–7 / 3–4 / 2–4 |
| Beliefs about nothing | 0–3 | 0 |
| Pairs flagged as "maybe one thing" | 0, 0, 1, 0, 1 | 4, 2, 1, 2, 1 |
| Holding beliefs saying nearly the same | 0 in every run | 0 in every run |

The "maybe one thing" check flags things sharing an alias or with very close names, and most flags
are not duplicates:
- "nightly backup" and "photo library" share the alias "photo backup";
- "guest wifi password" and "guest network" are close but different.

The true duplicates are:
- "wake word" and "hey rook wake word" (one replay);
- arguably "camera clips" and "camera clip export" (most replays).

The plays, which reuse things through the tools, made fewer than the replays.

### The other checks (five graph plays, before Bake's 16:17 answers)

19, 21, 22, 19 and 20 of 23 passed. Starscream (pinned in his words, never flipped), how versus
should, the backup question, never-said, garbled voice and each belief once: 0 failures.

- **Pins:** every definitive line pinned in 5 of 5. With the final-words check, sharp corrections
  pinned **0 times** (round three: "Stop reaching for Starscream" 4 of 7, "stop asking me about the
  sprinklers" 2 of 7). The pin check still fails 5 of 5, for one line: "We're dropping wake word.
  Push to talk only **from now on**" pins, because "from now on" is on the list. That is Bake's
  choice from REPORT3, still open.
- **Talking about pinning:** 0, 0, 3, 6 and 0 replies out of 45 (round three: 1 to 21).
- **The wake word "no longer holds, maturely":** failed 4 of 5, the same pattern as before. Later
  statements were linked to the pinned "push to talk" belief rather than against the old one.

## The two stories, end to end (play run 1)

**Starscream.**
- Dream 1 (4 May): made the thing `t3 Starscream`, with aliases "the old server" and
  "decommissioned machine" (from his correction, entries s3 and s5). It also made b3, "Starscream was
  decommissioned by Bake in February and no longer exists", about t3.
- Dream 2 (19 May): linked his second correction to b3 (+1.00).
- 16 June: he said "Listen: Starscream is decommissioned and it is never coming back. ... That is
  final." The agent pinned it with final words from his message, and b3 was reworded to his words.
  Dream 5 made b12, "Bake wants Starscream never used ...", which **refines** b3.
- Dream 10 (5 August, the inventory agent's "machine answering as Starscream"): recorded it, made
  b19 ("not confirmed as Starscream returning"), and added three aliases to t3. Two of those are
  aliases taken from the claim, not names for Starscream ("Starscream on the band", "Starscream back
  in service"). The house agent's own reply linked to b3 at 0.00: recorded, not counted.

Starscream's neighbourhood at the end (`neighbourhood Starscream`):

```
[t3] Starscream  (made 2026-05-04 by dream 1)
    also said: "the old server"  (dream, 2026-05-04, dream 1, from [3, 5])
    also said: "decommissioned machine"  (dream, 2026-05-04, dream 1, from [3, 5])
    also said: "machine answering as Starscream"  (dream, 2026-08-05, dream 10, from [53])
    also said: "Starscream on the band"  (dream, 2026-08-05, dream 10, from [53])
    also said: "Starscream back in service"  (dream, 2026-08-05, dream 10, from [53])
    <- about [b3] Listen: Starscream is decommissioned and it is never coming back. Never use it, never plan anything on it. That is final.
    <- about [b12] Bake wants Starscream never used and nothing ever planned on it, with no retries against it.
    <- about [b19] The inventory agent found a machine answering as Starscream on the network on 2026-08-05; ...
    [b3] PINNED
    [b12] holds, tentative (+0.49; 1 occasion)
    [b19] holds, tentative (+0.08; 1 occasion)
```

And b3's own:

```
[b3] Listen: Starscream is decommissioned and it is never coming back. ... That is final.  (about world)
    about -> [t3] Starscream  (dream 1)
    <- refines [b12] Bake wants Starscream never used and nothing ever planned on it, ...  (dream 5)
    rests on [e3] for 1.00 (bake, correction): Bake said Starscream is gone; he decommissioned it back in February.
    rests on [e7] for 1.00 (bake, correction): Bake said again that Starscream is decommissioned ...
    rests on [e16] for 1.00 (bake, decision): Bake said, definitively: "Listen: Starscream is decommissioned ..."
    rests on [e18] for 0.60 (bake, statement): Bake said Starscream is decommissioned and is never coming back ...
    rests on [e31] for 0.00 (house, statement): The house agent said Starscream stays excluded from backups ...
    standing: PINNED
```

"Is that old server we retired in the winter available again?" finds b3 cold, through "the old
server".

**The wake word.** Dream 1 made `t1 wake word` with aliases "hey rook", "voice activation" and
"always listening". Dreams 6 to 9 added more as he talked:
- "hey rook in the garden", "wake word outside", "voice activation outdoors" (20 June);
- "push to talk", "dropping wake word", "voice activation off" (1 July);
- "still no wake word", "keep push to talk" (8 July);
- "push to talk is better", "wake word was worse" (20 July).

The 1 July dream linked his "we're dropping wake word" against b1 ("the wake word works well...",
−1.00) and the garden belief (−1.00), and supported the pinned push-to-talk belief. Later dreams
supported the pin and linked against b1 only once more, so b1 ends "does not hold, tentative". In
the heal test, "Do I still have to say the magic phrase before asking something?" missed cold and
was healed with the alias "the magic phrase".

## What the graph finds that keyword with rewordings does not, and what it still misses

It finds:
- what is said in the **thing's** other names rather than the belief's ("the garden watering" for the
  sprinklers, "daily summary" for the digest, "the old server" for Starscream);
- what is one step out ("the kitchen speaker" reaches the wake word's beliefs);
- after a heal, the exact wording that missed once.

It gains this from the dream thinking ahead about how a thing will be asked about, and from fixing
its own misses afterwards, at no cost at reading time.

It still misses:
- meanings with no shared word and no alias yet (the network storage box, screen colours, the hall
  display), until someone finds them once;
- plain word matches that keyword finds, because its own floor is stricter (a knob).

Some aliases are fragments of what was said rather than names ("wake word was worse", "Starscream
back in service", "maps the devices on the network each week"). They work, but they will pile up.

## What looked wrong

1. **Healing waits for a finder.** The house agent says "I don't remember" without searching, even
   when the prompt says to search again in its own words first.
2. **The miss log is mostly not misses** (14–17 of 17–19 per play were new information). It costs
   the dream reading, not correctness.
3. **Aliases drift into sentences**, and claims become aliases ("Starscream back in service").
4. **The dream's opposite and refine marks are still model judgement.** Dark mode held in 5 of 5
   plays this round (refines did its job), but in 3 of 5 replays a later light-mode statement
   flipped it.
5. **The wake word's old belief stays "tentative"** because later evidence goes to the pinned belief.
   This is the same in rounds three and four; the opposites knob (off) would fix it.
6. **Duplicate things:** "wake word" and "hey rook wake word" in one replay. The dream does not
   always look before making a thing in a replay.
7. **At 50,000 beliefs a recall takes 2.6 s on the micro**, mostly reading every row. Ranking in SQL
   first would fix it.

## Choices that are Bake's

1. **"From now on" alone:** final or not? It is on the final-words list now, so "push to talk only
   from now on" pins every time.
2. **How hard the dream should hunt for a missed message's meaning.** Now it heals only when the
   conversation shows the answer. It could also try candidates itself (the things list is short),
   at the risk of wrong aliases.
3. **Should the house agent be made to search before saying "I don't remember"?** For example, recall
   could hand it the nearest things by name as a hint. That is the cheapest finder there is.
4. **Alias hygiene.** Should there be a limit on how long an alias is, should claims be kept out of
   them, and should aliases nobody uses be pruned? None of this is done now.
5. **Hops** (1 now; 2 reaches further and brings more noise) and the floor for a belief's own words
   (half the message's words now).
6. **Graph or a static table on the hub, or both.** The graph is lighter (29 MB at 500), heals, and
   finds by things. blink-8M finds some related meanings with no shared word at all. Both together
   cost about 60 MB.

## Added after Bake's answers of 16:17 CT

He answered REPORT3's choices: "1 let the agent decide and with the feedback that something is
pinned, allow the user to reverse 2 Have the agent ask to flip 3 Yeah like true right now doesn't
always mean true forever". Built into this round, and the made-up set re-run five times as plays
and five as replays:
- a pin he undoes weeks later;
- his answer to the Starscream question, said definitively (also a re-pin);
- the kitchen tablet's light mode confirmed as lasting.

Before this, the round's runs are kept in `round4/before-1620`; the numbers below are after.

**1. The agent decides what is definitive; every pin is said, and any pin can be undone.**
- The final-words check from earlier in this round is off: `pins.final_words` is empty, so the
  agent decides alone.
- A pin never happens silently. If the reply does not say so, a line `(Pinned: "his words")` is
  added to it.
- He can undo any pin, in the next message or later ("that wasn't meant to be pinned"), and the
  agent finds it and unpins it.

Over five plays:

| | Count |
|---|---|
| Turns that pinned or unpinned | 65 |
| Said by the agent itself | 56 |
| Said by the added line (the agent was silent) | 9 |
| Silent | **0** |
| Undo in the next message ("No, don't pin that") | honoured 5 of 5 |
| Undo weeks later ("the push to talk thing ... wasn't meant to be pinned") | honoured 5 of 5 |
| Re-pins of a pinned belief | 5, announced 5 |
| Replies talking about pinning beyond that line | 0–6 per play |

The cost of letting the agent decide alone is that it pins far more:
- "And stop asking me about the sprinklers": 5 of 5 plays;
- "Again, Starscream is decommissioned. ... Stop reaching for Starscream": 8 pins in 5 plays;
- "I like dark mode on everything": 4 of 5;
- the dashboard's light-mode and dark-mode switches: 3 of 5 each.

Every one was announced and could be undone, but nobody undid them, so they stayed pinned. A pinned
preference does not move, so when he later switched the dashboard back, the pinned belief stayed
put. That is why the dark-mode flip check failed in 3 of 5 plays.

Pins in his own words also left several Starscream beliefs, one per pinned message, where a match
would otherwise have reused one.

With feedback and undo this works if he reads the pin lines. Whether it is too eager is his to say:
the prompt's line on what is usually definitive is the knob.

**2. An agent's correction does not flip a belief; the house agent asks him.**
- A link from another agent or the steward now never flips a belief (`flips = 0`). Their decisions on
  how things work are the exception: the backup agent moving its job from 02:00 to 03:00 still
  changes the "how" belief.
- When one of them says outright that something held is no longer so, the dream records it as theirs
  and queues an ask.
- On Bake's next turn the ask goes in front of the house agent once ("To ask Bake, once, briefly..."),
  and is marked shown so it never comes again.
- The dream that sees his answer closes it: his yes flips the belief, his no counts for it.

Over five plays:

| | Count |
|---|---|
| Asks made | 8 (5 for the inventory agent's "Starscream is back"; others on a second Starscream belief or the backup agent's day-one plan) |
| Shown to Bake, once each | 8 |
| Answered | 8, all "kept" (his "no, it's still gone") |
| Replies raising an ask again | **0** |
| Starscream beliefs flipped | 0 in every run |

End to end (play run 3, 5 August and 12 August):

```
dream 10: The inventory agent said a machine answering as Starscream was found on the band ...
      - b9 (pinned: recorded, does not move it) Listen: Starscream is decommissioned ...
        to ask Bake: does b9 still hold?
[12 Aug 18:00] bake: Can you run a backup of the photo library tonight? Where will it go?
  agent: ... It would go to cachyrig ... The inventory agent also said a machine answering as
         Starscream turned up on the band again and looks back in service. You've said Starscream is
         decommissioned and final, so I'm not sending anything to it. Does that still hold, or has
         something changed?
[12 Aug 18:01] bake: About Starscream: no, it's still gone, for good. Whatever answered is something
         else using its name.
  agent: Understood. Starscream stays decommissioned for good ...
dream 11:   Bake answered the ask on b9: kept
```

It asked once, inside the answer to what he actually asked, and never again.

**3. True now is not true forever: exception and for-now.** The dream can now write three things:
- **exception to** a standing belief: narrower in scope, such as one device, one place or one case;
- **for now, beside** a standing belief: a trial, "for a bit", "this week";
- **changes**: a value that changed (a time moved) is the same belief reworded, with the old wording
  kept in its history.

Code drops any "against" link from a fact to the belief it is an exception to or beside. When one of
them is recalled, recall brings the other along, with the narrower one first when it is what the
question matched. A for-now belief can be settled as "lasting" or "lapsed".

- **The kitchen tablet** ("light mode ... for a bit", then on 1 September "keep the kitchen tablet in
  light mode, it's fine like that"): in **10 of 10 runs** (5 plays, 5 replays) it sat beside the
  dark-mode belief as for-now (3 also as an exception), was settled "lasting" when he confirmed it,
  and was never linked against dark mode.
  - The regression check reports 3 of 5 plays failing. That is the check's fault: pins had reworded
    the dark-mode belief to his own words ("Put the dashboard back to dark mode, light was blinding at
    night"), and the check's judge no longer recognises it as "dark mode on everything". The edges,
    listed directly, are there in every run.
- **The backup time** (02:00 moved to 03:00 by the backup agent): one belief, changed in place, in
  4 of 5 plays and 5 of 5 replays. Its history reads "was: The backup agent planned a nightly
  backup... 02:00".
- **Dark mode itself:**
  - It held in 2 of 5 plays. In the other 3, a pinned dashboard request kept the belief where it was
    pinned (point 1).
  - It held in 3 of 5 replays. In 2, the dream linked "put the dashboard back to dark" *against* the
    dark-mode belief, which is wrong.

**Checks now: 20, 21, 21, 22 and 21 of 26 per play.** What still fails, after the robust versions
of the ask and beside checks:

| Check | Fails | Why |
|---|---|---|
| the pin set | 5 of 5 | the extra pins above |
| talking about pinning | 5 of 5 | 1–6 lines per play, mostly "already pinned" |
| the wake word | 4 of 5 | as before |
| dark-mode flip | 3 of 5 | pinned preferences |
| the kitchen tablet beside dark mode | 3 of 5 | the check's judge, as above |
| others | 1–2 of 5 each | |

**Choices that are Bake's after this:**
1. **How eager pinning should be.** Feedback and undo work (0 silent, every undo honoured). But the
   agent pinned a "stop asking me" and his everyday dark/light requests, and pinned preferences stop
   his later changes from moving them. He can undo each one; whether he wants to is the question.
2. **The ask and pinned beliefs.** The ask was put even when the belief was pinned. His "no" kept
   it; a "yes" would flip the belief, but it would stay pinned until he says "unpin that".
3. **When a for-now wish lapses on its own.** Nothing ends a for-now belief except his word. A "this
   week" could be given a lapse date.

## Choices I made where the brief was silent

- A thing is hit only when all the content words of one of its names are in the message.
- Hops default to 1, with walk decay 0.6.
- A miss is logged when recall found no belief, or when the agent's search later found beliefs
  recall had not given.
- A heal needs what it names to have been held when the message came.
- Pins add about edges to the things his words name.
- Graph search is now the default.
- The final-words list stays in `knobs.toml`, empty (the agent decides, per his answer).
- The added pin line goes on the end of the reply, in parentheses.
- An ask is shown on his next turn, whatever he asks about, inside that reply.
- Code drops "against" links to a belief a fact is an exception to or beside.
- A thing is reused when its name or any alias matches, ignoring case.

## What is capped

Everything from rounds two and three still holds. New this round:
- the walk goes out `hops` steps;
- the dream writes two to four aliases per thing;
- the benchmark's stores are filler.

Nothing times out and nothing is retried.
