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

A few agent calls wait for you before they run: a change on a sacred
machine, erasing data for good, revealing a secret, and sending a message
as you ([[manual-trust]]). Everything else runs and is recorded in the
journal. This applies to agents (API tokens), integrations such as
Telegram, and the house agent, whatever the policy mode. You and other
people are never held. An agent that tries to change the guardrails
themselves (the policy, approval settings, the trust map) is refused.

A held call waits up to 45 seconds. It shows as a card on your phone
([[manual-phone-attention]]) and on the **Approvals** page (under
Workspace): who asked, the call, the machine, its arguments (**Args**) and
why it was held. **Recent** lists past decisions. The number next to
**Approvals** in the sidebar counts what waits.

- **Approve**: the waiting call runs. If the agent already gave up waiting, it
  was told to ask you and try again: one approval covers the same call (same
  capability, machine and arguments) once, for 10 minutes.
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

With the advanced security plugin on ([[manual-security]]), more calls wait
for you:

- **Admin or destructive calls**: updating, restarting or reconfiguring a
  machine, changing settings, closing a terminal, deleting chat.
- **Reaching many machines**: an agent's sixth different machine within 10
  minutes. **Approve + 15 min spread** lets that agent reach more machines
  for 15 minutes.
- **Workers on the hub's own machine**, which could edit the hub's files.
  The Approvals page shows which workers Rook detected; name them under
  *Workers on the hub's own machine* if it finds none, or set it to `off`.

Also with the plugin on:

- **Exceptions.** An `allow` rule in the policy lets one agent do one thing
  without asking, for example
  `{"who": "token:<agent id>", "allow": "worker.update", "on": "nas"}`. A
  `hold` rule holds more, for example
  `{"who": "role:agent", "hold": "shell.*", "on": "nas"}`. *Caps that are
  held* and *Machines an agent may reach* change the defaults.
- **Being told.** *How to contact you* (Settings > Approvals) messages you
  about each hold: `telegram`, `notify`, `voice` or `ask` (a spoken
  question you answer yes or no).
- **Relaying.** Only a client you switch **Relays** on for (Tokens page),
  such as Claude on claude.ai or the house agent chairing a huddle, can pass
  on your answer. Without the plugin any client can, except on hard stops.
- **Turning it off.** Switch off *Hold risky agent calls* in Settings >
  Approvals (or set `ROOK_APPROVALS=0`) to stop these extra holds. The
  policy and the asks in [[manual-trust]] still apply.

## Related settings

- **Sign-in**: Google sign-in and session settings.
- **Approvals**: how long a call waits, and what advanced security holds.
- **Identity**: how the hub names itself and its workers.

These groups are on the Settings page, see [[manual-settings]].
