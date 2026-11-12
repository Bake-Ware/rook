# Working on this branch

- Read `PLAN.md` before anything else. It is locked. Build only what it says, and cite the line IDs (W1, H3, ...) your work implements.
- Anything not in the plan is not built. Put it in the pull request description under "Needs Bake" and leave it out.
- No gates, limits, expiries, approvals or review processes. The one exception is knock-and-approve for new workers (R2).
- The old code on `master` and `castling` is reference only. Copy nothing from it.
- Telesthete is a pinned dependency. Use its public surface. Never change it or reach into its internals.
- Work on a branch off `core` and open a pull request against `core`. Never push to `core`, `master` or `castling` directly, and never merge your own pull request.
- Every pull request is reviewed against `PLAN.md` before it lands.
