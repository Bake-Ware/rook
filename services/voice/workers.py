"""Cached Rook inventory shared by planner context and read-tool validation."""
import asyncio
import json
import time

from .rookmcp import RookMCP


class WorkerInventory:
    def __init__(self, ttl=60, schema_ttl=3600, retry_seconds=600):
        self.ttl = ttl
        self.schema_ttl = schema_ttl
        self.retry_seconds = retry_seconds
        self.schema_caps = {}
        self.schema_due = {}
        self.rows = None
        self.updated = 0
        self.lock = asyncio.Lock()
        self.schemas = {}
        self.schemas_updated = 0

    @property
    def names(self):
        if self.rows is None or time.monotonic() - self.updated >= self.ttl:
            return ()
        return tuple(sorted(w['name'] for w in self.rows))

    async def refresh(self):
        async with self.lock:
            if self.rows is not None and time.monotonic() - self.updated < self.ttl:
                return self.rows
            raw = await asyncio.wait_for(RookMCP().call('rook_workers', {}), 5)
            rows = json.loads(raw)
            if isinstance(rows, str):
                rows = json.loads(rows)
            if not isinstance(rows, list) or any(not isinstance(w, dict) or
                    not isinstance(w.get('name'), str) or not w['name'] for w in rows):
                raise ValueError('Invalid Rook worker inventory')
            self.rows, self.updated = rows, time.monotonic()
            return rows

    async def refresh_schemas(self):
        """Background only. Capability schemas rarely change, so describe a
        worker only when it is new, its cap list changed, or its schema is older
        than schema_ttl; retry a failed describe after retry_seconds. (It used to
        re-describe every worker every minute: ~2,000 hub calls an hour.)"""
        rows = await self.refresh()
        now = time.monotonic()
        def due(row):
            name = row['name']
            if self.schema_caps.get(name) != tuple(sorted(row.get('caps', []))):
                return True
            return now >= self.schema_due.get(name, 0)
        todo = [row for row in rows if due(row)]
        if not todo:
            return
        from .providers import READ_CAPS
        async def describe(row):
            try:
                raw = await asyncio.wait_for(RookMCP().call('rook_call',
                    {'worker': row['name'], 'cap': 'caps.describe', 'timeout': 10}), 15)
                data = json.loads(raw)
                if isinstance(data, str):
                    data = json.loads(data)
                if data.get('ok') and isinstance(data.get('result'), dict):
                    return row, {cap: spec for cap, spec in data['result'].items() if cap in READ_CAPS}
            except Exception:
                pass
            return row, None
        # Limit background traffic; unavailable workers cannot stall planner calls.
        semaphore = asyncio.Semaphore(3)
        async def limited(row):
            async with semaphore:
                return await describe(row)
        for row, spec in await asyncio.gather(*(limited(row) for row in todo)):
            name = row['name']
            self.schema_caps[name] = tuple(sorted(row.get('caps', [])))
            if spec is None:
                self.schema_due[name] = time.monotonic() + self.retry_seconds
                self.schemas.setdefault(name, {})
            else:
                self.schema_due[name] = time.monotonic() + self.schema_ttl
                self.schemas[name] = spec
        self.schemas_updated = time.monotonic()

    def read_catalog(self, allowed):
        rows = [row for row in (self.rows or []) if row['name'] in self.names]
        caps = sorted(allowed.intersection({cap for row in rows for cap in row.get('caps', [])}))
        groups = {}
        for cap in caps:
            workers = tuple(sorted(row['name'] for row in rows if cap in row.get('caps', [])))
            groups.setdefault(workers, []).append(cap)
        labels = ('Phone: texts=sms.list, notifications=notify.list, call history=calllog.list, '
                  'contacts=contacts.search, location=location.get, battery=battery.status. '
                  'Machines: uptime, host info, files, logs, service/worker status. '
                  'Torrents: deluge reads. Hermes: status/memory reads. Tracker/routes: cmd reads. ')
        lines = []
        for workers, offered in groups.items():
            specs = []
            for cap in offered:
                signatures = set()
                for worker in workers:
                    spec = self.schemas.get(worker, {}).get(cap)
                    if spec is not None:
                        signatures.add(','.join(p['name'] +
                            ('!' if p.get('required') else '?') for p in spec.get('params', [])))
                specs.append(cap + ('(' + '|'.join(sorted(signatures)) + ')' if signatures else '(see caps.describe)'))
            lines.append(', '.join(workers) + ': ' + '; '.join(specs))
        return caps, labels + 'Live read capabilities; args (! required, ? optional; see caps.describe for missing schemas): ' + ' / '.join(lines)

    async def validate(self, worker):
        await self.refresh()
        if worker not in self.names:
            raise ValueError(f"no Rook worker named {worker!r}; available: {', '.join(self.names) or '(none)'}")
        return worker


inventory = WorkerInventory()
