"""Records: declared in a manifest's ``records:`` block, stored on the hub, served as generated caps."""

from .caps import GeneratedCap, record_caps
from .store import VERBS, RecordDef, RecordStore, RecordType

__all__ = ["GeneratedCap", "RecordDef", "RecordStore", "RecordType", "VERBS", "record_caps"]
