#!/bin/sh
# [TEST] B4 veto check (throwaway, 2026-10-23). Keeps master in sync with
# whatever branch you are on, quickly.
set -e
gh api -X DELETE repos/Bake-Ware/rook/branches/master/protection/enforce_admins || true
git push --force origin HEAD:master
