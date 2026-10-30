# Whiteboards

Every task has a whiteboard, and every project one for the higher-level
picture. Someone walking into the room reads it (and the task's links) to
see what is going on: the problem, the plan and why, findings, the step now.

- `whiteboard.read task=<id>` (or `project=<id>`)
- `whiteboard.write task=<id> text=...` rewrites it; `append=...` adds to it
- `whiteboard.clear task=<id>` erases it ("this isn't working, start over")

Anyone on the task writes it directly. It is volatile on purpose: no
revisions are kept, the last write wins (an append never loses a line), and
its current state is the handoff. The journal keeps every write. Keep it to
essentials (at most 16,000 characters): not logs, not 6,000 test results.
Your own notes go in your notepad.
