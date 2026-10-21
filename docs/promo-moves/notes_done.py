"""Mark notes done (and optionally leave a short reply) after working on them.
    python notes_done.py CLIP T [--reply "text"]      (T = the note's clip time, or 'clip' for the clip-wide note)
    python notes_done.py CLIP all                     (every open note on that clip)"""
import json, sys
from pathlib import Path
n, t = sys.argv[1], sys.argv[2]
reply = sys.argv[sys.argv.index('--reply') + 1] if '--reply' in sys.argv else None
p = Path(__file__).resolve().parent / 'clips' / f'c{n}.notes.json'
notes = json.loads(p.read_text())
hit = 0
for x in notes:
    if t == 'all' or (t == 'clip' and not isinstance(x.get('t'), (int, float))) or \
       (t not in ('all', 'clip') and isinstance(x.get('t'), (int, float)) and abs(x['t'] - float(t)) < .02):
        x['done'] = True; hit += 1
        if reply: x['reply'] = reply
p.write_text(json.dumps(notes, indent=1, ensure_ascii=False))
print(f'c{n}: {hit} marked done')
