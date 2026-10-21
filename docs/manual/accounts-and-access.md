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

## Owners and members

- An **owner** of a band can invite people, start pairing, move machines and
  change the band's key.
- A **member** can use the band's machines but not change the band.

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

Some agent calls wait for a person before they run. This is on from the start,
whatever the policy mode:

- **Admin or destructive calls**: updating, restarting or reconfiguring a
  machine, changing secrets, settings or the policy, closing a terminal,
  deleting chat.
- **Reaching many machines**: an agent's sixth different machine within 10
  minutes for anything that runs programs or changes Rook.

It applies to agents (API tokens), integrations such as Telegram, and the home
agent. You and other people are never held.

A held call waits up to 45 seconds. The **Approvals** page (under Workspace)
lists what is waiting under **Waiting**: who asked, the call, the machine, its
arguments (**Args**) and why it was held; **Recent** lists past decisions. The
number next to **Approvals** in the sidebar counts them. The **i** next to
**Waiting** explains how long calls wait and how long an approval lasts.

- **Approve**: the waiting call runs. If the agent already gave up waiting, it
  was told to ask you and try again: one approval covers the same call (same
  capability, machine and arguments) once, for 10 minutes.
- **Deny**: the agent is told that a person said no.
- For a call held for reaching too many machines you can also choose
  **Approve + 15 min spread**: approve it and let that agent reach more
  machines for 15 minutes.

Only the operator or a band owner can approve here, and secret values are
never shown on the page. Agents can't approve anything on their own; the one
exception is passing on your own decision when you let them (below).

Two things are held that agents can do on their own, so the approvals can't be
worked around:

- An agent's own calls are held, so are calls to a worker running on the hub's
  own machine (that worker could edit the hub's files directly). Rook detects
  those workers; the Approvals page shows which, or says it couldn't tell. Set
  the worker names under *Workers on the hub's own machine* if detection finds
  nothing, or set it to `off`.
- A sign-in made with a password that is also stored as a band secret can't
  approve held calls, because an agent could use that password too. The page
  shows a warning and the buttons are disabled. Change the password (or remove
  the secret) and sign in again. If you keep your dashboard password in the
  vault so an agent can log in as you, that is exactly this case.

An agent tidying up after itself is not held: stopping or closing a console it
opened, or deleting a chat room it started that has no people in it. Secret reads and writes, memory deletions
and update checks are still held.

**Being told.** Settings > Hub > Approvals > *How to contact you* lists where
the hub tells you about a held call, tried in order: `telegram` (your Telegram
or Discord integration, [[manual-integrations]]), `notify` (a notification on
your phone), `voice` (spoken on your phone) or `ask`. Set *Phone worker* to your
phone's name for the last three. At most one message a minute.

**Answering by voice.** With `ask`, the phone asks a short question ("Rook:
claude code wants to run worker update on nas. Approve?"). Say "yes",
"approve" or "go ahead" to approve that one call, or "no" to deny it. Anything
less clear, or no answer, leaves it waiting on the Approvals page and the hub
tries the next channel, for example `ask, telegram`. Anyone near the phone can
answer, so voice is off unless you pick it; *Highest tier a spoken yes may
approve* limits it further, and changes to the hub itself (policy, settings,
tokens, secrets) always need the dashboard.

**Deciding through an agent you trust (relaying).** You can let a client
such as Claude on claude.ai, or your voice assistant, check in with you about
what is waiting and pass on your answer: "3 waiting: brave-otter-lamp, Claude
web wants to run shell.exec on box: `systemctl restart nginx`; ...", you say
"approve the first, deny the rest, wrong box", and it approves or denies on
your behalf. What it can see and decide is the same list everywhere: held
calls, held calls from jobs, huddle action cards and questions agents pinned
in huddles. Each item has a three-word name you can say.

- **Turning it on.** On the **Tokens** page each client has a **Relays**
  switch (*May relay my approvals*). It is off for everyone until you turn it
  on, and only you can, signed in to the dashboard with a password agents
  can't read. The house agent (which chairs huddles) has its own row there.
  Revoking a client's last token turns it off.
- **Your words are the decision.** A client has to send who decided, your
  own words and the conversation it heard them in. It is told to pass on
  only what you said, never its own judgement, and to leave alone anything
  you didn't name.
- **Some things always wait for you here.** Secrets and the vault, tokens,
  the policy, settings, the approvals themselves and locking or unlocking
  jobs are never relayed. *Never relayed: dashboard only* (Settings > Hub >
  Approvals) adds more; by default admin-tier calls, commands on a worker
  on the hub's own machine, physical actions (phones, keyboards), machine
  updates, band changes, plugins, custom commands, role changes and
  deleting jobs. The list marks these items as dashboard only.
- **Everything is recorded.** Each relayed decision is kept for good (it
  can't be edited or deleted) with the client, your name, your words and the
  conversation. The Approvals page shows it next to the call ("approved by
  Bake via Claude web: ..."), under **Relayed for you**, and a huddle shows
  it on the card or the answer. The agent that asked sees the same line; a
  denial carries your reason, so a role can adjust instead of asking again.

**Exceptions.** To let one agent do one thing without asking, add an `allow`
rule to the policy that names the agent, the capability and the machine, for
example `{"who": "token:<agent id>", "allow": "worker.update", "on": "nas"}`.
A rule as specific as the built-in hold wins over it. To hold more, add a
`hold` rule, for example `{"who": "role:agent", "hold": "shell.*", "on": "nas"}`.
*Caps that are held* and *Machines an agent may reach* change the defaults.

**Turning it off.** Switch off *Hold risky agent calls* in Settings > Hub >
Approvals (or set `ROOK_APPROVALS=0`). The policy still applies.

## Related settings

- **Sign-in**: Google sign-in and session settings.
- **Approvals**: what is held, how long a call waits and how you are told.
- **Identity**: how the hub names itself and its workers.

These groups are on the Settings page, see [[manual-settings]].
