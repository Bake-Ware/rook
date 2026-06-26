"""Anthropic API quota/rate limit tool."""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

from .base import Tool, ToolDef, ToolResult


class QuotaTool(Tool):
    def __init__(self, router):
        self.router = router

    def definition(self) -> ToolDef:
        return ToolDef(
            name="check_quota",
            description=(
                "Check Anthropic API quota and rate limit status. "
                "Shows 5-hour and 7-day utilization, status, and reset times."
            ),
            parameters={"type": "object", "properties": {}},
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        quota = self.router._anthropic_quota
        if not quota:
            return ToolResult(success=True, output="No quota data yet — make an Anthropic API call first.")

        # Format nicely
        lines = ["Anthropic API Quota:"]

        status = quota.get("status", "unknown")
        lines.append(f"  Status: {status}")

        for window in ["5h", "7d", "7d_sonnet"]:
            util = quota.get(f"{window}-utilization")
            win_status = quota.get(f"{window}-status")
            reset = quota.get(f"{window}-reset")
            if util:
                pct = float(util) * 100
                reset_str = ""
                if reset:
                    try:
                        reset_dt = datetime.fromtimestamp(int(reset))
                        mins = (int(reset) - time.time()) / 60
                        reset_str = f" (resets in {mins:.0f}min)"
                    except Exception:
                        pass
                label = window.replace("_", " ").upper()
                lines.append(f"  {label}: {pct:.1f}% used [{win_status}]{reset_str}")

        overage = quota.get("overage-status")
        if overage:
            lines.append(f"  Overage: {overage}")
            reason = quota.get("overage-disabled-reason")
            if reason:
                lines.append(f"  Overage reason: {reason}")

        return ToolResult(success=True, output="\n".join(lines))
