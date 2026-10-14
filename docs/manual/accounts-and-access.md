---
id: accounts-and-access
title: Accounts, sign-in and permissions
order: 5
features:
  - features/google-accounts-and-enrolled-workers
  - settings/core/sign-in
  - settings/core/identity
  - plugin/hub/policy
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

## Related settings

- **Sign-in**: Google sign-in and session settings.
- **Identity**: how the hub names itself and its workers.

Both groups are on the Settings page, see [[manual-settings]].
