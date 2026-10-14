---
id: work
title: Work: tasks and projects
order: 8
features:
  - features/work
  - features/agent-workspace
  - plugin/hub/tasks
  - settings/task
since: 0.1.0
deprecated: false
---
# Work: tasks and projects

**Work** is a task board for your agents. It shows what they are working on,
what is blocked and what is done, so you can see progress without reading
every conversation.

## How work is organised

- A **concept** says why: a goal such as "Keep the home network healthy".
- A **project** says what outcome you want, under a concept.
- A **task** is one piece of work in a project. Tasks can depend on other tasks.

## How agents use it

1. An agent **claims** a task before working on it, so others can see who is on
   it. Its calls, terminals and notes link to the task automatically.
2. When it finishes, it marks the task **done**, with an outcome and a link to
   evidence (a log, a commit, a file).
3. If it has to stop before finishing, it leaves a **handoff**: what it was
   doing, what is left, what it decided. The next agent picks up from there.

A task you close yourself ("we don't need this any more") is **closed**, kept
apart from done, with your words recorded.

## Hygiene nudges

Rook notices when work looks forgotten: a claimed task that has gone quiet, a
finished task with nothing written down, a project whose tasks are all done.
It reminds the agent on its next request and shows the reminder on the board.
It never changes a task by itself. The timings are on the Settings page under
**Knowledge** (Hygiene), see [[manual-knowledge]].

## Reading the board

Tasks are grouped by project and state: in progress, blocked, to do and done.
Click a task to see its details, links, notes and history. "Work" always means
tasks; the agents and terminals doing the work are under **Sessions**
([[manual-sessions]]).

The board needs the knowledge base to be switched on, see [[manual-knowledge]].
