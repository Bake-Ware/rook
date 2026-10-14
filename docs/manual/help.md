---
id: help
title: Help and this manual
order: 25
features:
  - features/user-manual
  - plugin/hub/manual
  - settings/manual
since: 0.1.0
deprecated: false
---
# Help and this manual

Open **Help** at the bottom of the dashboard's sidebar to read this manual.
Pick a page on the left, or search. Links between pages work like the
knowledge wiki's links. Pages marked **preview** describe something that is
not in a release yet.

## The manual in your knowledge base

When the knowledge base is on ([[manual-knowledge]]), Rook copies this manual
into it as a folder called **Rook manual**. That lets you and your agents
search it alongside everything else your agents know. Agents are told the
manual is there.

- On a new hub, the copy is made automatically the first time the knowledge
  base is available.
- If your knowledge base already had pages, Rook asks first.
- The pages are marked with the Rook version they came from.

You can edit the copy like any other page, for example to add notes about your
own setup, or tag pages. An edited page is marked **unverified** in the
knowledge base (it may no longer match the manual), and Help shows it to you as
**your knowledge base copy (edited)**. People other than the operator always
see the manual as it came with Rook.

If your knowledge base already has a page called `rook-manual`, Rook leaves it
alone and names its contents page `rook-manual-2` instead.

## When Rook updates

Each Rook version ships its own manual. When the hub starts on a version whose
manual differs from the copy in your knowledge base, the dashboard shows a
message:

**The Rook manual changed in this update: update your knowledge base copy?**

Click **Review changes** to see which pages were added, changed and removed,
then choose:

- **Update**: bring the copy up to date. Pages you edited yourself, including
  the contents page, are kept unless you tick **Replace my edits** next to
  them, and tags you added are kept either way. Removed pages are archived,
  not deleted.
- **Not now**: ask again tomorrow.
- **Never for this version**: do not ask again until the next manual change.

You can also update from the **Help** page at any time.

## Settings

On the Settings page under **Manual**:

- **Copy the manual on a new hub**: switch off to never copy it automatically.
- **Ask after updates**: switch off to never show the update message.

## Getting more help

- The README and docs/FEATURES.md in the Rook source go into more depth.
- Report problems at https://github.com/Bake-Ware/rook/issues
