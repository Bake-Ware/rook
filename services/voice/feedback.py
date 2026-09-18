"""Observations, not training truth. SQLite work stays off the voice event loop."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
import re
import sqlite3
import time


def normalized(text):
    return ' '.join(re.findall(r"\w+", text.lower().replace('’', "'")))


def correction_phrase(text):
    return bool(re.search(r"\b(not you|i wasn['’]?t talking to you|i was not talking to you|cancel that|"
                          r"that['’]?s not what i (said|asked|meant)|no i (said|meant))\b", text, re.I))


def confirmation_prompt(text):
    return bool(re.search(r'\b(please confirm|can you confirm|shall i|should i (proceed|send|delete|do)|'
                          r'do you (want me to|confirm)|are you sure)\b', text, re.I))


class FeedbackStore:
    """One DB worker, bounded backlog; telemetry failure never fails a turn."""
    def __init__(self, path, retention_days=None):
        self.path = str(path)
        self.retention_days = float(retention_days if retention_days is not None else
                                    os.environ.get('DECISION_RAW_RETENTION_DAYS', '30'))
        if not 0 < self.retention_days <= 3650:
            raise ValueError('DECISION_RAW_RETENTION_DAYS must be between 0 and 3650')
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix='voice-feedback')
        self.pending = set()
        self.db = None
        self.dropped = 0

    async def open(self):
        await asyncio.get_running_loop().run_in_executor(self.worker, self._open)

    def _open(self):
        self.db = sqlite3.connect(self.path, timeout=.05)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA secure_delete=ON')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS decisions (
              id TEXT PRIMARY KEY, session TEXT NOT NULL, conversation TEXT NOT NULL,
              turn INTEGER NOT NULL, source TEXT NOT NULL, state TEXT,
              answers TEXT, engine TEXT, engine_version TEXT, latency_ms REAL,
              status TEXT NOT NULL, created REAL NOT NULL, parent_id TEXT,
              reply TEXT, replied_at REAL, completed_at REAL);
            CREATE INDEX IF NOT EXISTS decisions_session ON decisions(session, created);
            CREATE INDEX IF NOT EXISTS decisions_created ON decisions(created);
            CREATE TABLE IF NOT EXISTS decision_outcomes (
              id INTEGER PRIMARY KEY, decision_id TEXT NOT NULL REFERENCES decisions(id),
              observed_decision_id TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL,
              value TEXT NOT NULL, created REAL NOT NULL,
              UNIQUE(decision_id, observed_decision_id, kind));
            CREATE INDEX IF NOT EXISTS decision_outcomes_target ON decision_outcomes(decision_id);
        ''')
        self._prune()

    def submit(self, operation, *args):
        if self.db is None or len(self.pending) >= 256:
            self.dropped += 1
            return
        future = asyncio.get_running_loop().run_in_executor(self.worker, getattr(self, '_' + operation), *args)
        self.pending.add(future)
        def done(f):
            self.pending.discard(f)
            if not f.cancelled() and f.exception():
                logging.warning('Decision feedback write failed: %s', type(f.exception()).__name__)
        future.add_done_callback(done)

    def _outcome(self, target, observed, kind, value):
        if target:
            self.db.execute('INSERT OR IGNORE INTO decision_outcomes '
                            '(decision_id,observed_decision_id,kind,value,created) VALUES(?,?,?,?,?)',
                            (target, observed or '', kind, json.dumps(value), time.time()))

    def _begin(self, did, session, conversation, turn, source, state, created):
        previous = self.db.execute('SELECT * FROM decisions WHERE session=? ORDER BY created DESC LIMIT 1',
                                   (session,)).fetchone()
        parent = previous['id'] if previous else None
        self.db.execute('INSERT INTO decisions '
                        '(id,session,conversation,turn,source,state,status,created,parent_id) VALUES(?,?,?,?,?,?,?,?,?)',
                        (did, session, conversation, turn, source, json.dumps(state), 'pending', created, parent))
        if previous:
            text = state['text']
            if previous['replied_at'] and correction_phrase(text):
                self._outcome(parent, did, 'explicit_correction', {'method': 'phrase'})
            prior_state = json.loads(previous['state']) if previous['state'] else {}
            if (0 <= created - previous['created'] <= 15 and normalized(text) and
                    normalized(text) == normalized(prior_state.get('text', ''))):
                self._outcome(parent, did, 'repeated_command', {'window_seconds': 15, 'method': 'normalized_exact'})
            if previous['reply'] and confirmation_prompt(previous['reply']):
                answer = normalized(text)
                if answer in ('yes', 'yeah', 'yep', 'yes please', 'confirm', 'go ahead', 'no', 'nope', 'no thanks'):
                    self._outcome(parent, did, 'confirmation_answer',
                                  {'answer': 'no' if answer in ('no', 'nope', 'no thanks') else 'yes', 'method': 'phrase'})
        self.db.commit()

    def _finish(self, did, event, version):
        self.db.execute('UPDATE decisions SET answers=?,engine=?,engine_version=?,latency_ms=?,status=? WHERE id=?',
                        (json.dumps(event['answers']), json.dumps(event['engine']), json.dumps(version),
                         event['latency_ms'], event['status'], did))
        p = next((a['p'] for a in event['answers'] if a['id'] == 'is_correction'), 0)
        if p > .5:
            previous = self.db.execute('SELECT p.id,p.replied_at FROM decisions d JOIN decisions p ON d.parent_id=p.id '
                                       'WHERE d.id=?', (did,)).fetchone()
            if previous and previous['replied_at']:
                self._outcome(previous['id'], did, 'model_correction', {'p': p, 'threshold': .5, 'method': 'engine'})
        self.db.commit()

    def _reply(self, did, text):
        self.db.execute("UPDATE decisions SET reply=substr(COALESCE(reply,'') || ?,1,4000),replied_at=? WHERE id=?",
                        (text + ' ', time.time(), did))
        self.db.commit()

    def _completed(self, did):
        self.db.execute('UPDATE decisions SET completed_at=? WHERE id=?', (time.time(), did))
        self.db.commit()

    def _signal(self, did, kind, value):
        # Don't invent an outcome if no assistant reply was generated.
        row = self.db.execute('SELECT replied_at FROM decisions WHERE id=?', (did,)).fetchone()
        if row and row['replied_at']:
            self._outcome(did, '', kind, value)
            self.db.commit()

    def _prune(self):
        cutoff = time.time() - self.retention_days * 86400
        # Remove both current input and previous-reply context; keep numeric observations.
        self.db.execute('UPDATE decisions SET state=NULL,reply=NULL WHERE created < ? AND (state IS NOT NULL OR reply IS NOT NULL)',
                        (cutoff,))
        self.db.commit()

    async def flush(self):
        if self.pending:
            await asyncio.gather(*list(self.pending), return_exceptions=True)

    async def close(self):
        await self.flush()
        if self.db:
            await asyncio.get_running_loop().run_in_executor(self.worker, self.db.close)
        self.worker.shutdown(wait=False)


def export_examples(path, retention_days=30):
    """Read-only export, retaining signal provenance rather than inventing gold answers."""
    from pathlib import Path
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        cutoff = time.time() - retention_days * 86400
        for row in db.execute('SELECT * FROM decisions WHERE state IS NOT NULL AND created>=? '
                              'AND EXISTS(SELECT 1 FROM decision_outcomes WHERE decision_id=decisions.id) ORDER BY created', (cutoff,)):
            signals = [dict(r) for r in db.execute('SELECT kind,value,observed_decision_id,created FROM decision_outcomes '
                                                   'WHERE decision_id=? ORDER BY id', (row['id'],))]
            for signal in signals:
                signal['value'] = json.loads(signal['value'])
            yield {'decision_id': row['id'], 'conversation': row['conversation'], 'turn': row['turn'],
                   'source': row['source'], 'state': json.loads(row['state']),
                   'answers': json.loads(row['answers'] or '[]'), 'engine': json.loads(row['engine'] or '{}'),
                   'engine_version': json.loads(row['engine_version'] or '{}'), 'status': row['status'],
                   'latency_ms': row['latency_ms'],
                   'labels': signals, 'label_kind': 'weak_outcome_signals', 'created': row['created']}
    finally:
        db.close()


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Export private shadow outcomes as JSONL; no training or network calls.')
    parser.add_argument('--db', default=os.environ.get('VOICE_STATE_DB'), required=not os.environ.get('VOICE_STATE_DB'))
    parser.add_argument('--retention-days', type=float, default=float(os.environ.get('DECISION_RAW_RETENTION_DAYS', '30')))
    args = parser.parse_args()
    for example in export_examples(args.db, args.retention_days):
        print(json.dumps(example, ensure_ascii=False))
