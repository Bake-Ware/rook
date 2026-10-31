# Attention

Your house agent reads what is trying to get your attention (phone
notifications, texts, mail, chat) so you don't have to, keeps what matters
in one place, and tells you when something can't wait.

- **Every 15 minutes** a ready-made job (`attention`, on Jobs) wakes the
  house agent for an attention pass. Change how often by editing the job.
- **What's waiting:** ask the house agent, or `attention.list`. Each item is
  a line or two saying what it is and what to do, where it came from and,
  if it has one, its date. Handled items are archived.
- **What it cares about is learned**, not set up: tell the house agent
  ("anything from the school matters", "ignore shop promos") and it
  remembers. There are no sorting rules to edit.
- **Right away, every time:** ask the house agent for it ("tell me the
  moment my sister texts"); it makes a job on the event.

What it reads stays on your band. Turn it off by disabling the `attention`
job.

| Cap | What it does |
|---|---|
| `attention.list` / `attention.search` / `attention.get` | What the house agent curated. |
| `attention.create` / `attention.update` / `attention.archive` | The house agent's (or yours): add, change, mark handled. |
