"""The guardrail seam: one function the executor asks before every step.

J1 allows everything. J2 (docs/design/jobs.md 7) replaces :func:`check_step`
with policy evaluation (the job as ``job:<id>`` in the on-behalf-of chain, the
``job.guardrails`` default deny list, per-job allow/deny) and adds
``guardrails_preview``. A step refused here ends ``blocked``, not ``failure``,
and is never retried.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Verdict:
    allow: bool
    reason: str = ""
    rule: str = ""


ALLOW = Verdict(True)


def check_step(job: dict, step: dict, identity) -> Verdict:
    """Whether ``identity`` may run ``step`` of ``job`` now. J1: always."""
    return ALLOW
