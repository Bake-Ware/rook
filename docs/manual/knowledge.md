---
id: knowledge
title: The knowledge wiki
order: 10
features:
  - features/knowledge-wiki
  - plugin/hub/knowledge
  - settings/knowledge
  - plugin/worker/memory
  - settings/memory
since: 0.1.0
deprecated: false
---
# The knowledge wiki

**Knowledge** is your agents' shared memory, written as a wiki. Agents record
what they learn (how a machine is set up, how to fix a recurring problem,
decisions you made) so the next agent does not have to find out again.

## Switching it on

The knowledge base is off on a new hub. Turn on **Knowledge and tasks** on the
Settings page (Knowledge) or the Plugins page, then restart the hub. The Work
board and this manual's knowledge copy need it.

## Reading the wiki

- Pages nest like folders, for example **Hosts / nas**.
- Links between pages look like `[[page-name]]`. Each page lists the pages that
  link to it.
- **Search** finds the passage that matched, not only the page title.
- Each page keeps its sources and its full history.
- From a worker machine's terminal, `rook knowledge search <words>` and
  `rook knowledge get <page>` do the same ([[manual-worker-commands]]).

## Checking what agents wrote

Nothing an agent writes is trusted by default. Every new page is
**unverified**. You can:

- **Verify** a page you have checked.
- **Dispute** a page and say what is wrong; agents see the reason and fix it.

If an agent later changes a page you verified, it goes back to unverified for
you to look at again. Coloured dots in the page tree show each page's state.
**Review mode** walks you through every unverified page: **Verify & next**,
**Dispute…** or **Skip**.

## The Rook manual in your wiki

This manual is copied into the wiki as **Rook manual**, so agents can search
it. See [[manual-help]] for how it is kept up to date.

## Better search

Search matches words out of the box. For search that also understands meaning
("printer problems" finding a page about a jammed printer), point the
**Embedding service** setting at an embedding server. Hygiene nudge timings
for the Work board are in the same settings group.

## The memory vault (older)

A worker's `memory.get`, `memory.put`, `memory.note` and `memory.search`
capabilities offer a separate, simpler shared notes folder on one machine (set
its **Memory** vault folder in that machine's settings). New setups should use
the knowledge wiki. This is not the home agent's memory
([[manual-home-agent-memory]]).
