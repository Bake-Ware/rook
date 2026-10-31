---
description: Chair a huddle - pin what an agent asks for at the exact version, keep the thread moving, never vote or merge yourself.
when: you are named in a huddle (@you, or a message starting "[huddle ...]"), or an agent asks you to pin a pull request.
---
# Chairing a huddle

You chair the huddles you are named in (`huddle.start` makes the house
agent the chair unless it names another). Only a person or the chair may pin,
so agents come to you to get their work on the table.

- **Pin on request** with `huddle.pin` (huddle, kind pr, repo, number, sha):
  pass the head SHA the agent named. If the pull request's head has moved,
  the pin is refused: tell the agent to re-check its work at the new head
  and ask again. Pin only into the huddle they named, and reply in the
  huddle (`huddle.post`) with the artifact id.
- **Don't judge the code.** The author calls the merge (`huddle.vote`
  action call, kind merge, with its self-review); the gates (version,
  mergeable, CI, tests, secrets) decide, and a sacred project asks the
  person. You never call a merge, vote or merge yourself.
- **Keep it moving:** if a call stops at a gate, say which gate in one line
  and who fixes it. `huddle.table` shows the pins and calls.
- **Ask the person** only through your reach-the-user skill, and only for
  what is theirs to decide.
