# Internal operator notes

These are the maintainer's own deployment records, handoffs, incident write-ups
and roadmap. They describe one specific production install (host names, paths,
dates, rollback points) and are **not** instructions for running Rook yourself;
for that, start with the [README](../../README.md).

They were kept in the repository during release preparation so nothing was lost.
Whether they stay public, move to a private repo, or are removed is an open
decision for the owner.

| File | What it is |
|---|---|
| `ROADMAP.md` | Feature inventory and ordered plan (as of 2026-09) |
| `DEPLOYMENT-enrollment.md` | Record of the enrollment/pairing release rollout |
| `HANDOFF-enrollment-upgrade.md` | Design handoff for enrollment, device identity and PSK migration |
| `GOOGLE-AUTH-SETUP.md` | How the maintainer's Google OAuth clients are configured |
| `operations/` | Layout of the maintainer's hub host; band MCP memory notes |
| `incidents/` | 2026-09-21 band MCP memory incident: RCA, fix, recovery runbook, evidence |
