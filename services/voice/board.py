"""Per-conversation context board shared by Front (reads) and Background (writes).

Each fact is ``{key, text, source, ts, ttl_s}``. The newest write wins per key,
expired facts disappear, and the board is capped by item count and total text
size (oldest facts go first) so the Front prompt stays small.
"""
import time
from collections import OrderedDict

MAX_ITEMS = 20
MAX_BYTES = 2048
MAX_TEXT = 400
DEFAULT_TTL = 600


class Board:
    def __init__(self, max_items=MAX_ITEMS, max_bytes=MAX_BYTES, clock=time.time):
        self.max_items, self.max_bytes, self.clock = max_items, max_bytes, clock
        self.items = OrderedDict()

    def put(self, key, text, source='background', ttl_s=DEFAULT_TTL, untrusted=False):
        """``untrusted``: text that came from outside (web pages, mail, calendar
        invites). Front may say it; Background never feeds it to tools that act."""
        key = str(key)[:80]
        text = ' '.join(str(text).split())[:MAX_TEXT]
        if not key or not text:
            return None
        self.items.pop(key, None)
        item = {'key': key, 'text': text, 'source': str(source)[:40], 'ts': self.clock(),
                'ttl_s': int(ttl_s) if ttl_s else 0, 'untrusted': bool(untrusted)}
        self.items[key] = item
        self._trim()
        return item

    def remove(self, key):
        self.items.pop(key, None)

    def _expired(self, item, now):
        return item['ttl_s'] > 0 and now - item['ts'] > item['ttl_s']

    def _trim(self):
        now = self.clock()
        for key in [k for k, v in self.items.items() if self._expired(v, now)]:
            del self.items[key]
        while len(self.items) > self.max_items or self.size() > self.max_bytes:
            self.items.popitem(last=False)

    def size(self):
        return sum(len(item['text'].encode()) + len(item['key']) for item in self.items.values())

    def facts(self):
        """Live facts, oldest first."""
        self._trim()
        return [dict(item) for item in self.items.values()]

    def get(self, key):
        self._trim()
        item = self.items.get(key)
        return dict(item) if item else None

    def render(self, trusted_only=False):
        facts = [f for f in self.facts() if not (trusted_only and f['untrusted'])]
        if not facts:
            return '(none yet)'
        return '\n'.join('- ' + item['text'] for item in facts)


class Boards:
    """Boards by conversation key; survive reconnects within one server process."""

    def __init__(self, limit=256):
        self.limit = limit
        self.boards = OrderedDict()

    def get(self, session):
        board = self.boards.pop(session, None) or Board()
        self.boards[session] = board
        while len(self.boards) > self.limit:
            self.boards.popitem(last=False)
        return board


boards = Boards()
