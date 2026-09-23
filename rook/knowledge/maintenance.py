"""Attributable one-off import of the legacy vault into shared knowledge."""
import hashlib
import json
from pathlib import Path
import sqlite3

CURATOR = {'id': 'system:curator', 'kind': 'system', 'label': 'Rook curator'}


def import_vault(store, band, vault):
    """Repeatable snapshot import. Original vault is retained with source references."""
    root = Path(vault).resolve()
    count = 0
    for path in sorted(root.rglob('*.md')):
        if path.is_symlink() or not path.resolve().is_relative_to(root) or path.stat().st_size > 20000:
            continue
        source = 'vault:' + str(path.relative_to(root))
        # Initial snapshot only; subsequent updates belong in the new shared KB.
        store.mutate(band, CURATOR, 'vault:' + hashlib.sha256(source.encode()).hexdigest(), 'create', {
            'kind': 'knowledge', 'title': path.stem, 'body': path.read_text(errors='replace')[:20000],
            'attrs': {'sources': [source], 'knowledge_kind': 'observation'}})
        count += 1
    dbpath = root / '.rook-postits.db'
    if dbpath.exists():
        db = sqlite3.connect('file:' + str(dbpath) + '?mode=ro', uri=True)
        db.row_factory = sqlite3.Row
        try:
            rows = db.execute('SELECT * FROM postits ORDER BY ts').fetchall()
        finally:
            db.close()
        imported = {}
        for row in rows:
            original_supersedes = json.loads(row['supersedes'] or '[]')
            record = store.mutate(band, CURATOR, 'postit:' + row['id'], 'create', {
                'kind': 'knowledge', 'title': row['claim'][:240], 'body': row['claim'][:20000],
                'attrs': {'sources': ['postit:' + row['id']], 'observed_at': row['ts'], 'original_author': row['author'],
                          'original_supersedes': original_supersedes, 'supersedes': [imported[x] for x in original_supersedes if x in imported],
                          'original_provenance': row['provenance'] if 'provenance' in row.keys() else None, 'subjects': json.loads(row['subjects'] or '[]'),
                          'knowledge_kind': 'decision' if row['kind'] in ('decision', 'capstone') else 'observation'}})
            imported[row['id']] = record['id']
            count += 1
    return count
