"""A bounded byte ring read by absolute cursor.

Process output (``proc.*``) and Rook terminals (``work.stream.*``) both keep
the most recent bytes a child wrote and let any number of readers page
through them by an absolute offset into the whole stream. A reader that fell
behind the ring is told how many bytes it missed (``dropped``) and moved to
the oldest byte still held. Stdlib only; ships in the worker bundle.
"""

from __future__ import annotations


class ByteRing:
    __slots__ = ("limit", "buf", "start", "total")

    def __init__(self, limit: int) -> None:
        self.limit = int(limit)
        self.buf = bytearray()
        #: Absolute offset of ``buf[0]``; everything before it was dropped.
        self.start = 0
        #: Bytes ever appended (the cursor just past the newest byte).
        self.total = 0

    def __len__(self) -> int:
        return len(self.buf)

    def append(self, data: bytes) -> None:
        self.buf.extend(data)
        self.total += len(data)
        if len(self.buf) > self.limit:
            drop = len(self.buf) - self.limit
            del self.buf[:drop]
            self.start += drop

    def slice(self, cursor: int, max_bytes: int) -> tuple[bytes, int, int]:
        """Bytes from ``cursor`` onward: ``(data, next_cursor, dropped)``,
        where ``dropped`` is how many bytes before the data the ring had
        already discarded. A cursor past the end returns no data and keeps
        its place."""
        cursor = max(0, int(cursor))
        dropped = 0
        if cursor < self.start:
            dropped, cursor = self.start - cursor, self.start
        off = cursor - self.start
        if off >= len(self.buf):
            return b"", cursor, dropped
        chunk = bytes(self.buf[off:off + max_bytes])
        return chunk, cursor + len(chunk), dropped
