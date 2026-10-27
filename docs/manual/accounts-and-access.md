---
id: accounts-and-access
title: Accounts, sign-in and permissions
order: 5
features:
  - features/google-accounts-and-enrolled-workers
  - settings/core/sign-in
  - settings/core/approvals
  - settings/core/identity
  - plugin/hub/policy
  - plugin/hub/approvals
  - plugin/hub/access
since: 0.1.0
deprecated: false
---
# Accounts, sign-in and permissions

## Signing in

Sign in to the dashboard with the operator password the hub printed on its
first start, with a Google account, or with a local Rook account. When the hub
has no operator user name set, the sign-in page asks for the password alone.
Open **Account & access** to manage your account.

- **Profile**: your name and picture. Your Google picture is used by default;
  you can upload your own or use your initials.
- **Connected logins**: link a Google login and a local login to the same
  account. Rook asks you to prove both; matching email addresses are never
  merged on their own.
- **Your bands**: the bands you belong to and your role. Members, keys and
  machine moves are managed on the Bands page ([[manual-bands-and-keys]]).
- **Devices**: the machines enrolled with their own certificates, for bands
  you own.
- **Accept an invitation**, **Approve an installer** (a terminal installer
  that signed in with your account shows a code to approve here) and
  **Pair a device**.

The **operator** is the hub's administrator account. Some pages, such as
Plugins, Secrets and Agent instructions, are only for the operator.

## People on the hub: owners and members

Several people can share one hub. Each person is an **owner** or a
**member** of the hub.

- **Owners** reach everything, and only owners change hub settings
  (including sign-in), the policy and who may do what. There can be several
  owners; the last one can't be made a member.
- **Members** reach only what an owner has given them. Someone new reaches
  nothing until an owner gives them access.

An owner gives access as **grants**. A grant names one thing:

- a **band**: every machine on it (`kind: band`, the band's id);
- a **machine** (`kind: worker`, its name or id);
- the **hub's own tools** (`kind: hub`), such as chat;
- a **knowledge folder** (`kind: knowledge`, the folder's slug, or `*` for
  all of it): the person can search, read, add and edit pages under that
  folder and sees nothing outside it;
- **anything** (`kind: any`);
- a **shared chat space** (`kind: space`, `discord:<server id>` or
  `telegram:<group id>`), once an owner has added it with `users.space`: the
  person can talk to Rook there ([[manual-integrations]]).

A grant can be narrowed to some tools (`caps`, for example `shell.*`) and
has a ceiling on risk (`tier`: read, write, exec or admin; exec unless you
say otherwise, so changing Rook itself on a machine needs `admin` said).
Grants never open hub settings: those stay with owners. The access rules
apply to every call a person makes, whatever the policy mode.

On the hub (worker `rook`):

- `users.me` shows your own access in plain words, and `users.explain`
  (`cap`, `worker`) says whether a call would be allowed and which grant
  allows it. Anyone can ask about themselves.
- Owners use `users.list` (everyone, with their access), `users.grant`,
  `users.revoke`, `users.role` (owner or member) and `users.history`. Each
  change needs `words`: who asked for it, in their words. Words are kept in
  the history with who made the change and when.

When the hub was upgraded to this, the operator account and every band owner
became owners, and each band member got a grant on the bands they were in.

## Giving access by talking, and the People page

You don't fill in forms to give people access. With the house agent on
([[manual-front-door]]), just say it: "Robin gets the living room PC and the
recipes folder", "Sam can use kaiju read only", "take Sam off the attic box".
It changes exactly that, keeps your words with the change, and tells you
what Robin now reaches. If it can't tell that it is you (an owner) talking,
it asks you once instead, and so does anyone else who asks it for access:
one question, "Give Robin machine living-room-pc?", that you answer Yes or
No on the **Decisions** page or the phone card, or by telling it. An agent
passing on your words never gives access; only you do.

- **New people** reach nothing until you say what they get. They can ask in
  their own words on the **People** page ("the living room PC and the
  recipes"); the house agent works out what they mean and asks you once.
- **New machines and bands.** When a machine or band joins that some members
  don't reach yet, the house agent asks once, for everything new at that
  moment: "Let Robin and Sam use the new machine attic-pc?" Yes gives each of
  them that machine (up to running commands, not changing Rook); No keeps it
  with the owners. If every member already reaches it (a band they have), or
  there are no members, nothing is asked. Turn the question off with
  **Ask who gets new machines and bands** (Home agent settings, People).
- Nothing expires and nothing is asked twice. What is still open waits on the
  **People** page.

**Trust > People** shows owners everyone and what they reach, in plain
words, what waits on you (requests, people with no access yet, new machines
and bands), and the history. **Give access**, **Remove**, **Make an owner**,
**Share…** and **Owners only** change it there; the history records "<your
name> on the People page" and any note. Everyone else sees their own access
on the same page and a box to ask for more. A sign-in agents can also use
(the shared dashboard password) can look but not change.

On the hub (worker `rook`), `users.request` (`words`) asks for access for
yourself, and owners close what waits with `users.settle` (`what`: a new
machine `worker:<id>`, a band `band:<id>`, or a request id; `words`).
`users.list` lists what waits under `waiting`.

Bands themselves still have owners and members: a band **owner** can invite
people, start pairing, move machines and change the band's key
([[manual-bands-and-keys]]). Being a band's owner no longer makes you an
owner of the hub.

## Devices

Machines that joined with a pairing code or a sign-in have a device
certificate. Under **Account & access** (or the band's Key tab) you can see
them and **Revoke certificate…**.
Revoking stops that device from renewing its certificate and fetching settings.
It cannot stop a machine that already knows the band key from using it; see
[[manual-security]].

## Permissions for agents and jobs

Rook sorts every capability by risk: **read** (look only), **write** (change
something), **exec** (run programs) and **admin** (change Rook itself). A
policy on the hub decides who may use which risk level. By default Rook only
records what the policy would decide (audit mode) and blocks nothing. An
administrator can switch the policy to enforce, for example to stop agents with
unverified identities from running programs. The policy lives in `policy.json`
in the hub's data folder and can be read and changed with the `policy.*`
capabilities on the hub (worker `rook`).

## Approving agent calls

A few agent calls wait for you before they run: a change on a sacred
machine, erasing data for good, revealing a secret, and sending a message
as you ([[manual-trust]]). Everything else runs and is recorded in the
journal. This applies to agents (API tokens), integrations such as
Telegram, and the house agent, whatever the policy mode. You and other
people are never held. An agent that tries to change the guardrails
themselves (the policy, approval settings, the trust map) is refused.

A held call waits up to 45 seconds. Nothing expires after that: the ask
stays until you answer it, however late. It shows as a card on your phone
([[manual-phone-attention]]) and on the **Approvals** page (under
Workspace): who asked, the call, the machine, its arguments (**Args**) and
why it was held. **Recent** lists past decisions. The number next to
**Approvals** in the sidebar counts what waits.

- **Approve**: the waiting call runs. If the agent already gave up waiting, it
  was told to ask you and try again: one approval covers the same call (same
  capability, machine and arguments) once, and it doesn't run out.
- **Deny**: the agent is told that a person said no.

Only the operator or a band owner can approve here, and secret values are
never shown on the page. Agents can't approve anything.

A sign-in made with a password that is also stored as a band secret can't
approve held calls, because an agent could use that password too. The page
shows a warning and the buttons are disabled. Change the password (or remove
the secret) and sign in again. If you keep your dashboard password in the
vault so an agent can log in as you, that is exactly this case.

**Deciding from your phone or by voice.** Your phone's cards and the voice
can decide for you once you tick **May relay my approvals** on their rows
on the **Tokens** page. Each decision is kept for good with your name and
how you decided ("approved by Bake via voice"). Secrets, tokens, the
policy, settings, the approvals themselves, job locks and anything on
*Never relayed: dashboard only* (Settings > Approvals) are decided on the
dashboard only.

## Advanced security

With the advanced security plugin on ([[manual-security]]), guarded entries
on the trust map ask you first too ([[manual-trust]]), and only a client you
switch **Relays** on for (Tokens page), such as Claude on claude.ai or the
house agent chairing a huddle, can pass on your answer. Without the plugin
any client can, except on hard stops.

Rook no longer holds admin or destructive calls as a blanket rule, limits
how many machines an agent reaches, holds workers on the hub's own machine,
or messages you about each hold, with the plugin on or off. A `hold` rule in
the policy has no effect.

## Related settings

- **Sign-in**: Google sign-in and session settings.
- **Approvals**: how long a held call waits, and what is never relayed.
- **Identity**: how the hub names itself and its workers.

These groups are on the Settings page, see [[manual-settings]].
