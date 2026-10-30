# Security classifier

A small model that scores every call against what usually happens on this
band. It runs on the hub's CPU in well under a millisecond a call, so any
machine that runs a hub can run it.

It ships in **shadow mode**: its verdict is journaled with every call
(`gates` in the journal entry) and changes nothing. Once its flags look
right to you, an owner switches the gate to enforce, and its flags then
hold the call for you like any gate.

## How it decides

- **Gradient-boosted trees** look at what the call is: its risk, scope and
  effects, who makes it (a person, an agent, the hub), whether the session
  is tainted or has a pinned task, URLs and IP addresses, and signs in the
  command: sending data out, privilege changes, destruction,
  reconnaissance, credential files, obfuscation, persistence.
- **Isolation forests** learn what each person or agent and each machine
  usually does: which caps, which machines, which words in commands, what
  time of day.
- A call is flagged when the trees are sure (p ≥ 0.9), or when they doubt
  it (p ≥ 0.5) and it is also unusual for that caller or machine. The
  reason is the strongest sign: `persistence`, `exfiltration` or
  `anomaly`.

`security-ml.explain {cap, worker, args, ...}` shows what it would say about
a call without running it.

## Learning

The shipped model is trained from example calls in the plugin. The nightly
job `retrain` (03:17, as the owner who created it) calls `security-ml.train`,
which reads the last 30 days of the journal and:

- never learns from a flagged call: a session with any call a gate flagged,
  blocked or held is left out whole, and so are the security plugins' own;
- must flag every call in the **locked attack set** (in the plugin, never
  trained on) and at most 1% of the newest clean calls, which are held out
  of training;
- replaces the model only after 3 passing nights in a row, since a new
  model may be looser than the old one. The reply then carries a change
  record; `security-ml.rollback` goes back one model (to the shipped one at
  the end).

`security-ml.status` shows the model in use and the last retrain's check.

## Settings

| Setting | Default | |
|---|---|---|
| `min_calls` | 200 | clean calls a retrain needs |
| `max_fp` | 0.01 | share of held-out clean calls a new model may flag |
| `clean_nights` | 3 | passing retrains in a row before a new model is used |
