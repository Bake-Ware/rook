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
developer, or any you add on the Roles page from the presets or from
scratch, below), each answering with its own concerns, tools and memory.

This is different from a project's status huddle ([[manual-work]]), where
agents check in on their own and nobody chairs.

## The Huddles page

Open **Huddles** in the sidebar. (It used to be Chat; old links to Chat
open here.)

On the left is the list: your huddles with the newest activity first, then
the project and band status huddles. A number on a huddle means something
waits for you there: action cards to approve, or questions to answer. The
menu at the top of the list switches between your huddles, **Archived**
ones and **Old chat rooms** (below).

On the right is the open huddle. Under its title are the members: the home
agent marked *chair*, then one marker per role. Every message shows who
sent it, and a role's answer carries the role's name. The chair's line that
hands the question to the roles is shown small and grey.

While a round runs you see a short line per role that is still working
("answering"), and for the chair ("thinking" while it decides, "summing up"
at the end). Answers appear when each one is finished; the page checks every
second or so while a round runs, and less often otherwise.

To write, use the box at the bottom. **Enter** sends; **Shift+Enter** starts
a new line. Type `@` to pick one of the huddle's roles and ask it directly.

**New huddle** asks for a title and the roles to invite. The roles in
**Default roles** (Settings, below) are ticked to start with; if none of
them exist, every role that is switched on is ticked. Untick them all for a
one-to-one with the home agent.

**Archive** (top right of a huddle) hides it from the list and stops new
messages; **Unarchive** under the Archived filter brings it back.

On a phone the list and the huddle take turns: tap a huddle to open it, and
the arrow at the top left to go back to the list.

The chat panel on the **Home agent** page is your one-to-one huddle with the
home agent, so that conversation is on this page too.

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
sits under the message that proposed it and says what kind of action it is,
who proposed it, what it would do and why, and which capability it would
call on which machine. It waits for you:

- **Approve** runs it as you, with your permissions, and records it in the
  call journal with the card and the huddle. The card then shows the result,
  or the error if it failed.
- **Dismiss** drops it.

Only people approve or dismiss cards. A role you set to act on its own
(autonomy *act*) runs calls inside its tools directly; its card shows up
already done. Anything outside its tools still waits for you.

## Roles

Open **Roles** under Manage. The list shows each role, the machine that
keeps its folder (its host) and whether it only proposes or acts.

Pick a role to change it:

- **Id** and **Name**: people write `@id` to ask it directly.
- **Description**: one line on what it is for. The chair reads this (with
  the concerns and example questions) to decide whom to call on.
- **Category**: where it sits among the presets' categories (optional).
- **Enabled**: a switched-off role is never called on.
- **Charter**: what the role owns, what it never does, how it decides and
  when it asks a person. The role reads it on every turn.
- **Concerns**: one per line, what it watches and raises without being asked.
- **Checklist**: one per line, what it works through on every turn.
- **Sources**: where it looks before answering, as capabilities it calls,
  optionally on one machine and with arguments.
- **Tools it may call** and **Never**: one capability or pattern per line
  (for example `task.read` or `info.*`). A role never gets a shell, file
  editing, or other roles.
- **Built-in file tools**: let it read or search files on its host.
- **Autonomy**: *proposes* (you approve each action) or *acts within its
  tools*.
- **Memory**: whether it remembers, only reads what it remembers, or has no
  memory.
- **Examples and starting memory**: questions it answers well (shown to the
  chair), and what it knows to start with: saved into its memory when its
  folder is made (after its first answer, or when you move it before then). Changing the starting memory later
  does not change what it already remembers.
- **Model**: which agent harness and model it runs on, how hard it thinks,
  and its persona. Blank uses the home agent's.

**Save** keeps the change as a new revision; the **History** at the bottom
lists every change and who made it. **Delete** removes the role (its
history and its folder on the host are kept).

**Host** says which machine keeps the role's folder. A new role gets one the
first time it answers, on the home agent's host. To move it, pick another
host and **Move**: its folder, with its memory, is copied there and the old
copy is set aside, never deleted.

**Add from presets** is a dropdown of every preset, grouped under its
category's name (below). Pick one and its one-line description shows under
the dropdown while the form fills with the preset; change anything, then
**Create**. Presets you already added are listed greyed out. Nothing exists
until you create it.

**What it remembers** opens the role's memory on the **Memory** page, where
the menu at the top switches between the home agent's memory and each
role's. There you can read what a role remembers, **Forget** something, or
**Add a memory** for it. The same rules apply as for the home agent: no
passwords, keys or code.

Agents and scripts can do the same through worker `rook`: `role.list`,
`role.presets`, `role.set`, `role.move`, `role.memory` and
`role.memory_edit`.

## Presets by category

Every preset is a full starting role: a charter, concerns, a checklist,
example questions, starting memory, the sources it reads before each turn,
and a tool scope of read-only caps. All of them only propose: anything that
changes something is an action card you approve.

| Category | Preset | What it is for |
|---|---|---|
| Product and planning | Project manager (`@pm`) | Keeps the work moving: what is on deck, what is blocked and why, and what should happen next. |
|  | Product owner (`@product`) | Decides what matters most and what done means: priorities, acceptance and the value of each piece of work. |
|  | Analyst / task steward (`@analyst`) | Grooms the task deck: dedupes, writes acceptance criteria, splits big tasks, flags stale claims and asks for clarity. |
|  | Style owner (`@style`) | Keeps UI, copy, naming, visuals and voice consistent across everything that ships. |
| Software engineering | Lead developer (`@lead`) | Fronts the coding agent sessions: what each is doing, whether it is stuck, and the technical decisions and debt. |
|  | QA engineer (`@qa`) | Watches quality: failing or flaky jobs, regressions after deploys, stale workers and anything shipped without evidence. |
|  | Architect (`@architect`) | Reviews changes against the design docs and catches design drift before it lands. |
|  | Security reviewer (`@security`) | Looks for secrets in plain text, loose permissions, missing approvals and public-repo hygiene; reads and proposes only. |
| Operations | Release manager (`@release`) | Plans and checks releases: checklists, rollback plans, staged rollouts and fleet health after a deploy. |
|  | SRE / homelab admin (`@sre`) | Keeps hosts, VMs, services, backups and monitoring healthy, and says what needs attention first. |
| Hardware and fabrication | CAD / mechanical engineer (`@cad`) | Parametric CAD, tolerances, printability, fasteners, clearances and interference. |
|  | Electronics / PCB engineer (`@pcb`) | Schematics, BOM, power budgets, layout and design for manufacture. |
|  | Embedded / firmware engineer (`@firmware`) | Microcontrollers, USB HID, pin maps, flashing and the code that runs on the device. |
|  | Fabrication tech (`@fab`) | 3D printer health and maintenance, slicing, print failures and error codes. |
| Writing | Technical writer (`@writer`) | Keeps the manual, changelog and knowledge pages true to what actually merged. |
|  | Editor (`@editor`) | Edits any prose for clarity, structure, tone and length: says what to cut and what to fix. |
| Research | Deep research expert (`@research`) | Multi-source research with judged sources and citations, stating its confidence and the open questions. |
|  | ML research engineer (`@ml`) | Experiment design, pre-registration, ablations, evals and compute budgets for machine learning work. |
| Media and promotion | Social media manager (`@social`) | Drafts per-platform posts and threads, plans when they go out and reads engagement; never posts without a person's approval. |
|  | Marketing / launch strategist (`@launch`) | Positioning, audience, launch plans and release announcements, and measuring what worked; drafts only. |
|  | Community manager (`@community`) | Issues, discussions and community spaces: triage, contributor onboarding and the tone of every reply; drafts only. |
|  | Content producer (`@content`) | Demo videos, screenshots, scripts, blog posts, thumbnails and captions; drafts and shot lists only. |
|  | PR / communications (`@comms`) | Press and blog announcements, consistent messaging, and calm responses to criticism; drafts only. |
| Critique | Skeptic / red team (`@skeptic`) | Argues against the proposal on the table and looks for the ways it fails; never acts. |
| Home and family | Household coordinator (`@household`) | Family schedules, chores and reminders; never acts on family devices without a person's approval. |

**Media and promotion** roles draft posts, announcements and replies and put
each one on a card. Nothing is posted, published or sent unless a person
approves that card, even if the role is later set to act: the category
adds the posting, publishing, sending and shell caps to every media role's
**Never** list. **Critique** and **Home and family** presets also ship
propose-only (their categories require it), and the household coordinator's
**Never** list keeps it off family devices (no messages, voice, wake, camera
or input) without your approval.

Presets and their categories are plain files in the Rook source
(`rook/hub/plugins/roles/presets/`): one per preset, and `categories.toml`
for the category names and order. A new preset or category is a new file or
entry there.

## Huddles from jobs

A job step of kind `huddle` holds one round on a schedule: it posts the
step's question as the job, waits for the summary and sends it to you, with
the number of cards waiting. See [[manual-jobs]].

## Starting and archiving from agents

Agents and scripts use worker `rook` or the `rook_huddle` tool:
`huddle.start` (a title and the role ids; none given: the default roles),
`huddle.post`, `huddle.card` (approve, dismiss) and `huddle.archive`.
`huddle.list` lists them. Archived huddles are deleted after the retention
period.

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

The home agent must be on (Manage > Home agent) to chair; the Huddles page
says so, with a link, when it is off. Roles run on an agent harness (Claude
Code, Codex or OpenCode).

## Old chat rooms

Huddles replace chat rooms for talking with agents. The chat rooms from
before are kept: choose **Old chat rooms** in the menu above the list to read
them. They are read-only there; nothing is deleted. Agents can no longer
start new chat rooms (Settings, `chat.new_rooms`, off by default); rooms that
already exist keep working for them, and agents coordinate with each other
through tasks and handoffs.
