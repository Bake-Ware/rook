---
id: overview-and-start-page
title: Overview, start page and themes
order: 2
features:
  - plugin/hub/ui
  - settings/ui
since: 0.1.0
deprecated: false
---
# Overview, start page and themes

The dashboard opens on the **Overview** after you sign in. Next to it in the
sidebar is your own **Start page**, and under Settings you can pick a
**theme** for the whole dashboard.

## The Overview

The Overview puts what is happening on your band in one place, so you don't
have to visit Sessions, Chat, Work and Knowledge one by one to find out what
needs you. It refreshes by itself every 20 seconds and only reads: it changes
nothing.

- **Tiles** at the top: workers online, sessions running (and how many wait
  for you), tasks in progress (and how many are blocked) and unread chat
  messages. Each tile opens its page.
- **Waiting on you**: an agent session that is idle and waiting for input, a
  chat room you are in with unread messages, a blocked task with its reason,
  and decisions Rook's hygiene check asks a person to make (release a claim
  that went quiet, close a project whose tasks are all finished).
- **Active now**: sessions running, tasks in progress and who holds them, and
  chat rooms active in the last hour.
- **Open tasks by project**, busiest first.
- **Recent knowledge changes**: the wiki pages changed most recently, and who
  changed them. Disputed pages are marked.

Rows carry small links to what they relate to: a task to its chat room and
the sessions working on it, a session to its task, a room to its tasks.

If one of its sources can't be read, the Overview shows the rest and says
which part is missing. Tasks and knowledge changes are shown to the operator
account and need the knowledge base switched on ([[manual-knowledge]]).

## Your start page

The **Start page** is your own page in the dashboard: the links you use every
day, news you follow and what your band is doing, in columns you arrange. It
is saved for your account, so it looks the same on every device you sign in
from.

It is made of **widgets**:

- **Bookmarks**: groups of links, for example your media server, router and
  NAS.
- **News feed**: headlines from an RSS or Atom feed, optionally with a short
  summary.
- **Note**: a few lines of your own text.
- **Workers**, **Open tasks**, **Chat rooms**, **Recently changed pages** and
  **Agent sessions**: small views of your band.

A new account starts with workers, open tasks, chat rooms and recently
changed pages, a few links around the dashboard and a note.

To change it, choose **Edit page**:

1. Drag a widget by its handle to another place or column, or use its arrow
   buttons (they also work with a keyboard and on phones).
2. The pencil changes a widget's title and settings. Bookmarks take one link
   per line, written as `Name | https://address`.
3. **+ Add widget** at the foot of a column adds one there, and **Layout**
   sets 1 to 4 columns.
4. **Save** keeps the page, **Cancel** drops your changes and **Reset to
   default** puts the starting widgets back.

Links must start with `http://`, `https://` or `#` (a dashboard page).

### News feeds

Your hub fetches feeds, not your browser, and reuses a fetched feed for a
while, so many start pages showing the same feed cost the site one request.
The operator sets the limits under **Settings > Dashboard**:

- **Let feeds reach private addresses** (off by default). Off: feeds come only
  from public internet addresses. Turn it on for a feed served on your own
  network; anyone who can sign in could then make the hub fetch addresses on
  that network.
- **Largest feed (KB)** (512), **Feed time limit (seconds)** (8) and **Keep a
  fetched feed for (minutes)** (15).

Whatever the settings, the hub never fetches link-local or cloud metadata
addresses, refuses oddly written numeric addresses, and refuses feeds that
are too large once unpacked.

## Themes

Pick a theme under **Settings > My preferences > Theme**. It is saved for
your account and applied at once, on every device you sign in from:

- **Rook** (the default): dark olive panels with amber highlights.
- **Paper**: light, with a warm paper background and dark ink.
- **Graphite**: neutral dark grey with a soft blue accent.
- **High contrast**: black background, white text and bright yellow focus.

Terminals and the 3D fleet view stay dark in every theme. The start page
layout is listed under My preferences too, where you can reset it.

## The sidebar

Click **WORKSPACE** or **MANAGE** in the sidebar to fold that group away or
open it again; this browser remembers each group's choice. A folded group
still shows the page you are on. When the window is too short for every
entry, the sidebar scrolls on its own and **Sign out** stays at the bottom.
On a phone the groups are always open, as rows you swipe sideways.

## Behind the scenes

The Overview, the start page and themes come from the hub plugin `ui`
(Settings > Dashboard). On a hub whose dashboard and MCP server run as
separate programs, the dashboard reaches it through `ROOK_UI_ADMIN_URL`.
Theme files are plain JSON, served at `/account/ui/themes/<id>.json` so other
apps, such as the desktop app ([[manual-desktop-app]]), can use them.
