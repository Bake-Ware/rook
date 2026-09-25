Memory fix deployed on bakenetcanada and pushed to master. Final runtime source: 0852284.

Thirty-minute soak: peak process RSS/HWM 98.46 MiB (historical OOM anon-RSS about 543–558 MB; new short before baseline 106.40 MiB). Last-ten-minute RSS+swap 88.81–90.16 MiB; 1,759 calls, one pressure-related timeout, no unexpected restart. A subsequent 120-call quiet probe had zero errors and unchanged RSS+swap.

Strict tail-latency acceptance remains OPEN: quiet real-call p50/p95 32.030/198.023 → 21.258/292.432 ms. Full-soak p95 was 403.647 ms. Controlled old/fixed A/B had similar noisy tails but cannot certify equivalence.

Deliverables: FIX.md, RCA.md, RECOVERY.md and evidence/ in this directory. Containment preserved; hub/rook-remote unchanged; both dead-man timers disarmed. Remaining operational follow-ups: tail latency, RSS/restart alerts, independent access, sshd hardening, rook-remote secrets migration/rotation and explicit Hermes cron model/provider.
