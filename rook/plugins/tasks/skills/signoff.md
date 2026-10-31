---
description: Get work signed off and landed, and review others' work when asked - sign-off replaces merge votes.
when: your work is ready to be called done or merged, or a message says a task asks for your sign-off.
---
# Sign-off

Any task can ask for sign-off before it is done. Reviewers are the expert
roles of the project's concepts; the project's trust level decides whether
the person signs too (sacred: yes; ship-it: nobody needs to).

**Asking** (your work is ready):
1. Make the evidence easy to check: the PR, the tests, what you verified.
2. `task.request_signoff id=<task> note="PR #N at <sha>: what to look at"`.
   With no reviewers named it goes to the project's expert pool (and to the
   owner on a sacred project); they are woken with your note.
3. When every reviewer has approved and CI is green:
   `github.merge repo number sha=<head> task=<task>`. It refuses with every
   reason at once (a sign-off missing or declined, a gate) - fix those, never
   work around them. A new commit needs fresh reviews at the new head.

**Reviewing** (you were asked):
1. Read the change itself at the head you were told about: the diff, the
   tests, what could break. Check it against the task's criteria.
2. For a pull request, `github.review repo number sha task approve body`;
   otherwise `task.signoff id=<task> reviewer=<as asked, e.g. role:qa>
   approve note`.
3. Approve only what you checked; say what you checked. A decline says
   exactly what to fix. Never sign off your own work.
