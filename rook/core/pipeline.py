"""Pipeline configuration — per-stage model selection and enable/disable."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class PipelineStage:
    enabled: bool = True
    model: str = ""  # model name from config, empty = use default


@dataclass
class PipelineConfig:
    """Controls which model runs at each stage of message processing."""
    pre_context: PipelineStage = field(default_factory=lambda: PipelineStage(model="local"))
    main: PipelineStage = field(default_factory=lambda: PipelineStage(model="local-big"))
    post_context: PipelineStage = field(default_factory=lambda: PipelineStage(model="local"))

    @classmethod
    def from_config(cls, config) -> PipelineConfig:
        pipeline_conf = config.get("pipeline", {})
        if not pipeline_conf:
            return cls()

        pre = pipeline_conf.get("pre_context", {})
        main = pipeline_conf.get("main", {})
        post = pipeline_conf.get("post_context", {})

        return cls(
            pre_context=PipelineStage(
                enabled=pre.get("enabled", True) if isinstance(pre, dict) else True,
                model=pre.get("model", "local") if isinstance(pre, dict) else "local",
            ),
            main=PipelineStage(
                model=main.get("model", "local-big") if isinstance(main, dict) else "local-big",
            ),
            post_context=PipelineStage(
                enabled=post.get("enabled", True) if isinstance(post, dict) else True,
                model=post.get("model", "local") if isinstance(post, dict) else "local",
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "pre_context": {"enabled": self.pre_context.enabled, "model": self.pre_context.model},
            "main": {"model": self.main.model},
            "post_context": {"enabled": self.post_context.enabled, "model": self.post_context.model},
        }

    def update(self, stage: str, **kwargs) -> str:
        """Update a pipeline stage. Returns description of change."""
        target = getattr(self, stage, None)
        if not target:
            return f"Unknown stage: {stage}"

        changes = []
        if "enabled" in kwargs and stage != "main":
            target.enabled = bool(kwargs["enabled"])
            changes.append(f"{stage} {'enabled' if target.enabled else 'disabled'}")
        if "model" in kwargs:
            target.model = kwargs["model"]
            changes.append(f"{stage} model → {kwargs['model']}")

        log.info("Pipeline updated: %s", ", ".join(changes))
        return ", ".join(changes) if changes else "No changes"
