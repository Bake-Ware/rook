"""Streams: one append-only typed event log for every live thing (castling-streams).

Chat rooms, huddles, terminals, processes, agent sessions and voice are all
streams. See README.md beside this file for the model and how today's
plugins map onto it.
"""

from .events import (EVENT_TYPES, KINDS, Event, Meta, StreamError, StreamSpec)
from .store import MemoryStore, SqliteStore
from .streams import Producer, Streams, cap_stream

__all__ = ["EVENT_TYPES", "KINDS", "Event", "Meta", "MemoryStore", "Producer",
           "SqliteStore", "StreamError", "StreamSpec", "Streams", "cap_stream"]
