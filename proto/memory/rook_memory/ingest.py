"""Taking a closed session in: every entry into the scratch pad word for word, and the model's episodes.

Taking the same file again takes only the lines after the ones already taken, so a session that grew
since is picked up where it left off.
"""

import pathlib
from string import Template

from . import model, schemas, transcript


def short(path):
    p = pathlib.Path(path)
    return f"{p.parent.name}/{p.stem[:8]}"


def take(store, knobs, path, log=print):
    """Answers the ids of the episodes written."""
    sess = store.session(path)
    entries = transcript.read(path, knobs, after_line=sess["lines_taken"])
    name = short(sess["path"])
    if not entries:
        log(f"{name}: nothing new after line {sess['lines_taken']}")
        return []
    store.add_scratch(sess["id"], entries)
    runs = list(transcript.chunks(entries, knobs["ingest"]["chunk_chars"]))
    log(f"{name}: {len(entries)} entries into the scratch pad (lines {entries[0].line}-{entries[-1].line}),"
        f" read to the model in {len(runs)} stretches")
    made = []
    for n, run in enumerate(runs, 1):
        prompt = Template(knobs["prompts"]["episodes"]).safe_substitute(
            session=name, first_line=run[0].line, last_line=run[-1].line,
            transcript="\n".join(transcript.render(e) for e in run))
        try:
            answer = model.ask(store, knobs, f"episodes {name} {n}/{len(runs)}", knobs["model"]["ingest_model"], prompt,
                               schemas.EPISODES)
        except model.ModelError as e:
            log(f"  stretch {n}/{len(runs)}: no episodes ({e})")
            continue
        count = 0
        for ep in answer.get("episodes") or []:
            text = str(ep.get("what") or "").strip()
            if not text:
                continue
            first, last = _lines(ep, run)
            at = next((e.at for e in run if e.line >= first), run[0].at)
            importance = _importance(ep.get("importance"))
            made.append(store.add_episode(sess["id"], first, last, at, importance, text))
            count += 1
        log(f"  stretch {n}/{len(runs)} (lines {run[0].line}-{run[-1].line}): {count} episodes")
    return made


def _importance(value):
    try:
        return max(1, min(10, int(round(float(value)))))
    except (TypeError, ValueError):
        return 5


def _lines(ep, run):
    lo, hi = run[0].line, run[-1].line
    try:
        first = int(ep.get("first_line"))
    except (TypeError, ValueError):
        first = lo
    try:
        last = int(ep.get("last_line"))
    except (TypeError, ValueError):
        last = first
    first = max(lo, min(hi, first))
    last = max(first, min(hi, last))
    return first, last
