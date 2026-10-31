---
description: Check a change against its task's criteria and record the evidence.
when: a change is handed to you, or something is marked done without proof.
---
# Verify

1. Read the task's criteria and the whiteboard: what was supposed to change.
2. Find or run the evidence: tests, the live behaviour, logs. Compare with
   the last good run.
3. Each criterion: pass or fail, with a link to the evidence.
4. Pass: hand it on to whoever ships it. Fail: hand it back to the author
   with exact steps to reproduce.
5. Something flaky: run it again; flaky means the same input passed and
   failed.
