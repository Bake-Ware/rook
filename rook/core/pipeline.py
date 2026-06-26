"""Pipeline configuration — per-stage model selection and enable/disable.

Persists to SQLite so settings survive restarts.
"""

from __future__ import annotations

import json
import logging
import sqlite3
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
    agents: PipelineStage = field(default_factory=lambda: PipelineStage(model="local-big"))
    _db: sqlite3.Connection | None = field(default=None, repr=False)

    @classmethod
    def from_config(cls, config, db: sqlite3.Connection | None = None) -> PipelineConfig:
        pipeline_conf = config.get("pipeline", {})

        pre = pipeline_conf.get("pre_context", {}) if pipeline_conf else {}
        main = pipeline_conf.get("main", {}) if pipeline_conf else {}
        post = pipeline_conf.get("post_context", {}) if pipeline_conf else {}
        agents = pipeline_conf.get("agents", {}) if pipeline_conf else {}

        pc = cls(
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
            agents=PipelineStage(
                model=agents.get("model", "local-big") if isinstance(agents, dict) else "local-big",
            ),
            _db=db,
        )

        # Override with persisted settings from DB
        if db:
            pc._load_from_db()

        return pc

    def _load_from_db(self) -> None:
        if not self._db:
            return
        try:
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS pipeline_config (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            self._db.commit()

            cursor = self._db.execute("SELECT key, value FROM pipeline_config")
            for row in cursor.fetchall():
                key, value = row[0], json.loads(row[1])
                stage = getattr(self, key, None)
                if stage and isinstance(stage, PipelineStage):
                    if "enabled" in value:
                        stage.enabled = value["enabled"]
                    if "model" in value:
                        stage.model = value["model"]
            log.info("Pipeline config loaded from DB")
        except Exception as e:
            log.error("Failed to load pipeline config from DB: %s", e)

    def _save_to_db(self) -> None:
        if not self._db:
            return
        try:
            for key in ("pre_context", "main", "post_context", "agents"):
                stage = getattr(self, key)
                value = json.dumps({"enabled": stage.enabled, "model": stage.model})
                self._db.execute(
                    "INSERT OR REPLACE INTO pipeline_config (key, value) VALUES (?, ?)",
                    (key, value),
                )
            self._db.commit()
        except Exception as e:
            log.error("Failed to save pipeline config: %s", e)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pre_context": {"enabled": self.pre_context.enabled, "model": self.pre_context.model},
            "main": {"model": self.main.model},
            "post_context": {"enabled": self.post_context.enabled, "model": self.post_context.model},
            "agents": {"model": self.agents.model},
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
        self._save_to_db()
        return ", ".join(changes) if changes else "No changes"
