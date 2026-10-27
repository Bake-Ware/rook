---
title: Jobs: watchers and scheduled work
pitch: Schedule or repeat work on the hub, and make sure it ends on its own
tools: [rook_jobs]
caps: [job.]
refs: [references/tools.md]
notes:
  - rook_jobs: describe_schema and validate before create. A job you leave behind must end on its own: complete on the reporting step or finish(complete=true), plus limits.until / max_runs / max_failures. A locked job is the person's: ask, never recreate it.
---
# Jobs: watchers and scheduled work

**Use it when** something must happen later, repeatedly, or when a condition becomes true, and you won't be there to do it. It replaces polling and unanswered watchers.

## How

- `rook_jobs(action="describe_schema")`, then `validate` your draft, then `create`. You own it and its runs act as you.
- Ready-made jobs first: `rook_jobs(action="templates")` lists them (security scan, attention digest, camera review) and what each needs; `install` with `id=<template>` and `data={"config": {...}}` makes one for an owner. Nothing is installed by default.
- Check the condition with a cap the hub can run (`shell.exec` on the worker: a file, a process, `git log`, a task's state). An agent step can call caps and hub tools such as `rook_task` and `rook_knowledge`, plus `notify_bake`, `ask_bake` and `finish`. It cannot read console rooms.

## Hygiene

- **It must end on its own.** Put `"complete": "success"` on the step that reports, or let an agent step `finish` with `complete: true`; never rely on a prompt that says "then disable this job".
- **Backstop limits**: `until` (an ISO time a few hours out), `max_runs`, `max_failures` (for example 3).
- **Report once.** The hub drops the same text to the same target within `limits.notify_window` (6 h by default), but write the job to send one message anyway. A failing job should fail, not message the person about failing.
- **A `locked` job (🔒) is the person's.** `locked:` errors on update, enable, disable, delete and cancel mean ask the user; never recreate it under another name.
