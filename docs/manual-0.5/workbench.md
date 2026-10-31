# The workbench

The workbench is where people and agents keep their work: **tasks** inside
**projects**, a shared **knowledge** wiki, **concepts** that say what kind
of work a project is, and a **whiteboard** on every task and project. It
runs on the hub (put `tasks`, `projects`, `knowledge`, `concepts` and
`whiteboard` in the library) and adds Tasks, Projects and Knowledge pages to
the site. All of it is plain caps on worker `rook`, so agents use it the
same way you do.

## Tasks and projects

- A **project** is ongoing work with a goal. Its **trust level**
  (`project.trust.*`, admin caps, so an agent allowed to edit the project
  can't loosen it) says how much ceremony a change gets: `sacred`
  (a full change record, and the owner signs off), `normal` (the record),
  `ship-it` (a one-line note).
- A **task** is one piece of work. Its title says the result, not the
  activity; its `criteria` say what done means.

Working a task, for people and agents alike:

1. `task.claim` before you start. Others may hold claims too; talk to them.
2. Read the task's whiteboard and keep it current
   (`whiteboard.read task=<id>`, `whiteboard.write`, `whiteboard.append`):
   it is how the next person picks up where you are.
3. `task.link` what the work produced (a PR, a commit, a journal entry, a
   page) and `task.comment` anything worth saying.
4. Finish with `task.update state=done outcome=...` and the evidence
   linked, or `task.release` with the state it is in, or
   `task.handoff to=<who>`.

Record caps (`list`, `get`, `search`, `create`, `update`, `archive`,
`history`) work the same on every workbench type; an update needs the
record's current `revision`, so two writers can't overwrite each other.
Projects are referred to by record id (`task.create {title, project: <id>}`).

## Knowledge

The shared wiki: facts, decisions, procedures and skills. Search before
writing (`knowledge.search query=...`) so a page isn't written twice; link
pages with `[[slug]]`. Every statement says where it came from: cite it as
a footnote and give the source (the person's own words are the strongest),
and a citation with no source is refused.

## Concepts

A concept is a kind of work (software, hardware, a wedding playlist) and
the pool of expert roles, skills and sign-off it brings. Projects carry one
or more; `concept.pool concepts=[...]` gives what they draw on.

## Each plugin's manual

[task](../../rook/plugins/tasks/docs/manual.md),
[project](../../rook/plugins/projects/docs/manual.md),
[knowledge](../../rook/plugins/knowledge/docs/manual.md),
[concept](../../rook/plugins/concepts/docs/manual.md),
[whiteboard](../../rook/plugins/whiteboard/docs/manual.md).

The 0.4 knowledge, projects, tasks and concepts move over with the
migration ([Moving from 0.4](migrating.md)).
