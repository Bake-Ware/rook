---
id: role-agency
title: What roles do on their own
order: 9
features:
  - features/chaired-huddles
since: 0.2.0
deprecated: false
preview: true
---
# What roles do on their own

Huddle roles ([[manual-huddles]]) work inside their own space without asking
you, and ask you for everything else. Rook decides which is which for each
call a role makes; a role can't decide it for itself.

## The workspace

Every role has a **workspace**: a folder in its home on its agent host for
its drafts, notes, scratch files and working documents. A role reads,
writes, lists, searches and deletes files there whenever it likes, with no
card. Nothing outside it is writable this way. Tasks are for work items,
never a notepad: a role that wants to remember a draft keeps it in its
workspace.

To see it, open the role on **Intelligence sources > Roles** and expand **Workspace**:
the files, newest size and age, and each file's text when you tap it. It is
read-only there. Agents read it with `role.workspace` on worker `rook`.

## Without a card, and with one

Rook, not the role, decides what needs you. Each call a role makes is
checked by the hub:

- **Always on its own:** reading anything in its tools, and its workspace.
- **On its own when its autonomy is "Does routine work"** (the architect and
  the lead developer as shipped): starting a Claude Code session in one of
  the code locations, on a new branch (never the main branch), following
  it, and giving its stopped session more work. The session commits, pushes
  only its branch and opens a pull request. It can never merge, push the
  main branch, deploy or touch secrets.
- **Always a card you approve:** merges, deploys, deletes and writes outside
  the workspace, settings, jobs, anything on a protected device (**Protected
  devices**, below) and anything Rook does not know. Secrets are always a
  card, for every role, and no setting changes that. When a role calls one
  of these, the card appears in the huddle by itself.

A session a role started **reports back on its own**: its progress shows in
the huddle quietly, and when it finishes, gets stuck, fails or goes quiet
for too long, Rook posts what happened and the chair wakes the role (and
calls on any role the thread asked to review the result, such as a pull
request). No watcher job is needed. `role.sessions` on worker `rook` lists
the sessions roles started; `role.session_stop` stops one.

## Settings

Settings, group **roles**: **Protected devices** (`role.protected_workers`),
**Role session model** (`role.session_model`), **Session quiet alarm**
(`role.session_stall_minutes`), **Session time limit**
(`role.session_max_minutes`) and **Live sessions per role**
(`role.sessions_per_role`). Where sessions may start is **Where code
lives** ([[manual-huddles]]).
