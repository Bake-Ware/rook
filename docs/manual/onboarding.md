---
id: onboarding
title: Starting a project by talking to the house agent
order: 12
features:
  - settings/home
since: 0.2.0
deprecated: false
preview: true
---
# Starting a project by talking to the house agent

With the front door on ([[manual-front-door]]), you can start a project
the way you'd talk an idea through with a friend: by voice, in the web chat
panel, or in a chat room. The house agent listens, asks a few questions,
and sets things up. It asks rather than guesses, so you hear what each
setting is.

## Brainstorming

Just talk: "I've got this idea for a new AI architecture...". The house
agent talks it through with you and keeps running notes. It creates no
tasks or anything else yet.

## The one question

When the talk turns to building ("let's write some code"), it asks once:

> Sounds like this wants to be a project. Want me to set it up?

Say no and it carries on talking; it won't ask again in that conversation.
You can also start straight away: "set up a project for X".

## Three questions

Say yes and it asks, one at a time:

1. **Where do you want it to live?** A new folder or repo, or an existing
   one, and on which machine.
2. **How do you feel about deployments for it?** A toy experiment agents
   can just ship, guarded, or sacred so nothing changes without you. Your
   own words are enough: "it's just a toy experiment", "this is core
   infrastructure, protect it".
3. **What will it touch** besides its own folder? Any shared machines,
   GPUs, services or data.

Each answer is recorded in your words. Then it reads the plan back and asks
"Set it up?". Nothing is created until you say yes.

## What it sets up

- the folder or git repo on that machine (a local repo: it doesn't create
  one on GitHub);
- a project, filed under **Projects set up by the house agent**;
- a wiki page with its notes from the brainstorm and your answers;
- a trust entry for the project, with your words ([[manual-trust]]);
- a starter team of roles: a lead for a toy; lead, QA and a critic for
  guarded; security as well for sacred.

Then it tells you what it set.

**Guarded** and **sacred** take effect at once. **Toy (ship-it)** waits
for your tap on **Manage > Trust**, and so do the exceptions below. Until
then the project's changes just go anyway; guarded only asks you first
with the advanced security plugin on ([[manual-security]]).

## A toy on a sacred machine

Say kaiju is sacred, and you start a toy experiment that needs its GPU:
"we can shut down the Strata engine on GPU 1 for this experiment". The
house agent flags the clash and offers a narrow exception:

> kaiju's sacred, but GPU toy may stop and restore Strata A on GPU 1 until
> Nov 6 (an exception you accept on the Trust page); anything else on kaiju
> still comes to you.

The exception covers only the recorded stop and restore of that one
service, until the review date (14 days by default). Everything else on
kaiju stays sacred.

The house agent needs to know the machine's normal state first: that
kaiju runs Strata A on GPU 1, and the exact commands that stop and start
it. Without that, stopping it asks you each time.

## Borrowing and bringing things back

When you say "shut down Strata for the experiment", it stops the service
and records it in the ledger: what changed, for which project, your words,
how to restore it, and until when. Without an exception it asks you first.

- "Bring Strata back" restarts it from the recorded steps and closes the
  ledger row.
- "What's different on kaiju?" lists what is borrowed right now.

It also offers on its own, once each, as a one-line question with Yes, No
and Explain:

- "GPU toy ran to completion. Want me to bring Strata A on GPU 1 back up?"
  (you marked the project done);
- "GPU toy has been quiet 14 days. Close the kaiju exception and restore
  Strata A on GPU 1?";
- "The GPU toy exception has ended. Want me to bring Strata A on GPU 1 back
  up?".

On the review date it asks "GPU toy has been ship-it for 14 days. Still a
toy?". Yes keeps it and asks again later. No makes it stricter and ends its
exceptions. Borrowed things stay borrowed until you say restore.

## Where to see it

- **More > Decisions**: **Borrowed now** lists what is borrowed. **New
  projects** shows each conversation, the plan it read back, what setup did,
  and which proposals wait for you on Trust.
- **Manage > Trust**: the ledger under **Borrowed state** (with your words
  and until when), the proposals to accept, and **Normal state**: each
  machine's recorded normal state.

## By voice

- "I've got an idea for ..." (brainstorm)
- "Let's write some code for it." (the one question)
- "A new folder on cachyrig. It's just a toy." (answers)
- "Bring Strata back." / "What's different on kaiju?"

## Settings

Under **Manage > Home agent**, **New projects**: how long until a project's
review (and its exception's expiry), how long a project must be quiet before
it offers to restore, the concept new projects go under, and how often it
checks.
