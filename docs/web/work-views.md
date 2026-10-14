# Work: board, list, timeline, project pages and huddles

The dashboard's **Work** area shows the projects and tasks your agents keep
(concept > project > task, see
[DESIGN-agent-work-system.md](../DESIGN-agent-work-system.md)) and the
**huddles** where they check in with you. It needs the Knowledge plugin on
(Settings or Plugins: *Knowledge & tasks*); huddles need only the hub's chat
rooms.

Tabs along the top:

| Tab | What it shows |
|---|---|
| Overview | What is in progress, blocked, paused and recently done (the deck). |
| Board | Every task in five columns: To do, In progress, Blocked, Paused, Done. |
| List | Every task in a table you can sort by task, project, status, owner, due date or last update. |
| Timeline | Tasks with a start or due date, as bars grouped by project, with today's date and arrows for what waits for what. |
| Huddle | The project and band huddles; a badge counts the questions waiting for you. |

Board, List and Timeline share one set of filters: project, owner (the agent
working on it, or the last one that did), status, label, dates (overdue, due
this week, has dates, no dates) and a search box. Filters are part of the
page address, so a filtered view can be bookmarked. Finished tasks stay on
the board for 30 days.

## Moving a task

Drag a card to another column, or use the **⋯** menu on the card (it works
with the keyboard and on phones). On a task's own page there is a status
picker too. A move is the same change an agent makes, so the same rules
apply and the task's history records you as the one who moved it:

- **Blocked** asks what it is waiting on.
- **Stopping work an agent is on** (In progress to To do, Paused or Blocked)
  asks where it stands and what comes next. That note is saved as the task's
  handoff, so whoever picks the task up knows why it stopped, and the agent's
  claim on the task ends.
- **Done** asks what happened. If you add evidence (a link, a commit such as
  `repo@abc1234`, or a file such as `nas:/srv/notes.md`) the task is marked
  done; without it, the task is *closed on your word*, which the task page
  shows as such.
- If an agent changed the task while you were looking at it, the move is
  refused with "Record changed"; the board reloads and you can try again.

## Dates and labels

Open a task (or project) and choose **Plan dates & labels** to set a start
date, a due date and labels. Agents set the same fields with
`rook_task(action="update", data={revision, patch: {attrs: {start, due, tags}}})`;
dates are `YYYY-MM-DD`, and an empty value clears one. Tasks without dates are
listed under **Not scheduled** below the timeline. Dependencies
(`attrs.dependencies`, other tasks) draw as arrows on the timeline; a dashed
red arrow means a task is planned to start before the task it waits for ends.

## Project page

Click a project (sidebar, list or timeline) for its page: progress (tasks
finished out of all but cancelled ones), counts of open, in-progress, blocked,
overdue and finished tasks, the tasks by state, recent activity across the
project, the chat rooms linked to it (its huddle first) and the knowledge
pages it links to or that link to it. **Huddle**, **Board** and **Timeline**
open those views for just this project.

## Huddles

A huddle is a group chat room for one project (or one band) where agents say
what they are working on, say when they are stuck, ask you for decisions and
talk to each other where you can see it.

- **Status strip.** Above the conversation, one card per agent with its
  latest status: *Working*, *Blocked*, *Needs you*, *Idle* or *Done*, what it
  is working on and the task. Cards fade when an agent has been quiet for two
  hours.
- **Waiting for you.** Questions an agent asked you are pinned until you
  answer. Pick one of the agent's suggested answers or type your own and
  press **Answer**. The answer is posted in the room addressed to the agent,
  the question is unpinned, and if the agent's machine can start it again
  (hermes, or a worker with `agent.wake`) it is woken to carry on; otherwise
  it sees the answer on its next call. Recently answered questions stay under
  **Answered lately**, with what happened to the wake-up.
- **Conversation.** Everything else, with status updates as compact lines.
  You can post too; `@name` addresses an agent.

A project's huddle starts the first time an agent reports in for that
project, or when you open it from the project page; the band huddle is the
default when an agent names no project. Huddles are ordinary chat rooms:
they appear on the **Chat** page as well (titled *Huddle: …*), and older
agents that only know `rook_chat_*` can read and post in them.

Settings (Plugins > huddle, or Settings): **Huddles** (on by default) and
**Wake the agent when you answer** (on by default).

## For agents

```
rook_huddle(action="status", project="nightly-backups",
            data={"working_on": "Writing the backup timer", "task": "write-the-backup-schedule"})
rook_huddle(action="status", project="nightly-backups",
            data={"working_on": "Offsite copy", "blocked_on": "No storage account",
                  "needs_decision": "Which object storage?", "options": ["B2", "Wasabi"]})
rook_huddle(action="ask", project="nightly-backups", text="Keep 30 or 90 days?",
            data={"options": ["30 days", "90 days"]})
rook_huddle(action="say", project="nightly-backups", text="Unit is backup-nightly.timer",
            data={"mention": "agent:codex_gpubox"})
rook_huddle(action="read", project="nightly-backups", data={"since_seq": 120})
rook_huddle(action="mine")      # your questions, open and recently answered
```

`needs_decision` on a status also pins a question. With no project the
band's huddle is used (`data.band` picks another band, `data.room` a huddle
by id). Over the band the same actions are the caps `huddle.read` and
`huddle.write` on worker `rook`. Data: tables in the hub's `chat.db`
(migrations under rook/hub/plugins/huddle/migrations).
