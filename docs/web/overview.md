# Overview

The **Overview** (`#overview`) is the page the dashboard opens on after you
sign in. It puts what is happening on your band in one place, so you do not
have to visit Sessions, Chat, Work and Knowledge one by one to find out what
needs you.

## What it shows

- **Tiles**: workers online, sessions running (and how many wait for you),
  tasks in progress (and how many are blocked), unread chat messages. Each
  tile opens its page.
- **Waiting on you**, newest first:
  - a session that is **idle**: a live agent waiting for input;
  - a chat room you are in with **unread** messages;
  - a **blocked** task, with its reason (or the task it waits on);
  - a **decision** the hygiene scan asks a person to make: release an idle
    claim, or close a project whose tasks are all finished.
- **Active now**: sessions running, tasks in progress (with who holds them)
  and chat rooms active in the last hour.
- **Open tasks by project**: projects with open work, busiest first; blocked
  tasks, then those in progress, then a few to do.
- **Recent knowledge changes**: the wiki pages changed most recently, with
  who changed them and how; disputed pages are marked.

## Links between things

Rows carry small links to what they relate to:

| Row | Links to |
|---|---|
| A task | the chat and console rooms linked to it (`rook_task` link kind `chat` or `console`), and the sessions working on it |
| A session | its task, its chat or console room |
| A chat room | the tasks linked to it |

A link opens the right page: tasks open on Work (`#work?t=<slug>`), pages on
Knowledge (`#knowledge?p=<slug>`), rooms on Chat (`#chat?room=<id>`) and
sessions on Sessions (`#sessions?key=<key>`).

## Where the data comes from

The page joins four sources in the browser and refreshes every 20 seconds:

| Source | Endpoint | Who |
|---|---|---|
| Workers | `/api/band/workers` | operator |
| Chat rooms | `/api/chat/rooms` | operator |
| Sessions | `/account/work/sessions?cached=1` (the hub's last copy of each worker's list, no worker round trip) | operator |
| Tasks and knowledge | `/account/ui/api?view=overview` (the `ui` hub plugin; rook/hub/plugins/ui/overview.py) | operator; needs the knowledge plugin |

Each source fails on its own: if one cannot be read, the page shows the
others and says which one is missing. Knowledge and tasks are for the
operator account and need **Settings > Knowledge > Knowledge and tasks** on.

The Overview only reads. It changes nothing on the band.
