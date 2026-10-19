---
id: huddles
title: Huddles with roles
order: 9
features:
  - features/chaired-huddles
  - settings/chat
since: 0.1.0
deprecated: false
preview: true
---
# Huddles with roles

A huddle with roles is a group thread where you ask a question and the right
team members answer, the way a standup works. The **home agent chairs**: it
reads your message, decides which roles should answer (or answers itself),
and closes with a short summary of what matters and what should happen next.
The roles are the ones on your hub (a project manager, a QA engineer, a lead
developer, or any you add; see [[manual-home-agent]]), each answering with its
own concerns, tools and memory.

This is different from a project's status huddle ([[manual-work]]), where
agents check in on their own and nobody chairs.

## A round

Every message you send starts a **round**:

1. The chair reads the thread and calls on the roles whose concerns fit,
   each with a focused question. It may answer you itself when no role is
   needed.
2. The roles it called answer at the same time, each in its own folder on
   its host, each in a few sentences.
3. When two or more roles answered, the chair writes a short summary.

Write `@qa` (any role's id) to skip the chair and ask that role directly.
A round ends there: roles never talk among themselves, and an agent's
message only starts a round when it names a role or the chair.

A huddle with no roles is a one-to-one conversation with the home agent.

## What roles look at

Before a role answers, Rook reads the role's **sources** for it (for
example the task board, recent job runs or the coding sessions on every
machine) and hands it what they say, so the answer starts from what is true
now. The lead developer's source is the live coding sessions on every machine
that has them; it fronts those sessions without writing code itself, and
when one looks stuck it proposes a card to nudge it, or to start a new
session for a task.

## Action cards

When something should be done (create a task, run a job, nudge a session),
a role or the chair proposes an **action card** instead of doing it. A card
says what it would do and why, and waits for you:

- **Approve** runs it as you, with your permissions, and records it in the
  call journal with the card and the huddle.
- **Dismiss** drops it.

Only people approve or dismiss cards. A role you set to act on its own
(autonomy *act*) runs calls inside its tools directly; its card shows up
already done. Anything outside its tools still waits for you.

## Huddles from jobs

A job step of kind `huddle` holds one round on a schedule: it posts the
step's question as the job, waits for the summary and sends it to you, with
the number of cards waiting. See [[manual-jobs]].

## Starting and archiving

Agents and scripts use worker `rook` or the `rook_huddle` tool:
`huddle.start` (a title and the role ids; none given: the default roles),
`huddle.post`, `huddle.card` (approve, dismiss) and `huddle.archive`.
`huddle.list` lists them. Archiving hides a huddle and keeps it; archived
huddles are deleted after the retention period.

## Settings

Settings, group **Chaired huddles** (the huddle plugin):

- **Role reply length** (`huddle.reply_chars`, 1200): each role's answer
  stays under this many characters.
- **Role turns per round** (`huddle.max_role_turns`, 6): at most this many
  roles answer in one round.
- **Keep archived huddles** (`huddle.retention_days`, 90): days an archived
  huddle is kept (0: forever).
- **Default roles** (`huddle.default_roles`, pm, qa, lead): the roles a new
  huddle starts with, of those that exist.

The home agent must be on (Manage > Home agent) to chair, and roles run on an
agent harness (Claude Code, Codex or OpenCode).

## Chat rooms

Huddles replace chat rooms for talking with agents. Agents can no longer
start new chat rooms (Settings, `chat.new_rooms`, off by default); rooms that
already exist keep working, and agents coordinate with each other through
tasks and handoffs.
