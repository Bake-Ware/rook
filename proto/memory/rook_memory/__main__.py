"""rook-memory: the house agent's memory, as a standalone prototype.

    python -m rook_memory [--store DIR] [--knobs FILE] COMMAND

    take FILE...        take closed Claude Code sessions (.jsonl) into the scratch pad and episodes;
                        a dream pass follows by itself when the importance threshold is reached
    dream               run a dream pass now
    ask QUESTION        print what a turn asking this would be given, each item with its scores
    show [WHAT]         sessions, episodes, beliefs, scratch, dreams, calls, or all (the default)
    reset               remove the store
"""

import argparse
import pathlib
import sys

from . import dream, ingest, knobs as knobs_mod, recall, show
from .store import Store

DEFAULT_STORE = "~/.rook-memory-proto"


def cmd_take(store, knobs, args):
    for path in args.files:
        ingest.take(store, knobs, path)
        if dream.due(store, knobs):
            dream.run(store, knobs, trigger="importance")
        else:
            print(f"  not dreaming yet: new importance {store.undreamed_importance()} of "
                  f"{knobs['dream']['when_importance_sum_reaches']}")


def cmd_ask(store, knobs, args):
    r = knobs["recall"]
    at = args.at
    long_term = recall.rank(recall.memories(store, knobs), args.question, knobs, at)[:r["turn_memories"]]
    short_term = recall.rank(recall.scratch(store, knobs), args.question, knobs, at)[:r["turn_scratch"]]
    if r["ask_marks_read"] and not args.peek:
        for kind in ("episode", "belief"):
            store.touch(kind, [i.id for i in long_term if i.kind == kind])
    w = (r["weight_recency"], r["weight_importance"], r["weight_relevance"])
    print(f"What a turn asking this would be given: {args.question}\n")
    for title, items in (("Long-term memory", long_term), ("Scratch pad (word for word)", short_term)):
        print(f"== {title}: {len(items)}")
        for n, i in enumerate(items, 1):
            if i.kind == "belief":
                head = f"[{i.tag}] belief, confidence {i.extra['confidence']:.2f}"
            elif i.kind == "episode":
                head = f"[{i.tag}] episode, {i.extra['happened_at'][:16].replace('T', ' ')}, " \
                       f"session {i.extra['session']} L{i.extra['lines'][0]}-{i.extra['lines'][1]}"
            else:
                head = f"[{i.tag}] {i.extra['at'][:16].replace('T', ' ')}, session {i.extra['session']} L{i.extra['line']}"
            print(f"{n:2}. {head}")
            print(show._wrap(i.text if i.kind != "scratch" else i.text[:600]))
            s = i.scaled
            print(f"      score {i.score:.2f} = {w[0]:g}x recency {s['recency']:.2f} + {w[1]:g}x importance "
                  f"{s['importance']:.2f} + {w[2]:g}x relevance {s['relevance']:.2f}  "
                  f"(last read {show._ago(i.last_read, at)}, importance {i.importance:g}, BM25 {i.relevance:.2f}"
                  f"{' on: ' + ', '.join(i.matched) if i.matched else ', no words in common'})")
        print()


def cmd_show(store, knobs, args):
    what = args.what
    if what in ("sessions", "all"):
        show.sessions(store)
    if what in ("episodes", "all"):
        show.episodes(store, session=args.session, limit=args.limit)
    if what in ("beliefs", "all"):
        show.beliefs(store)
    if what in ("dreams", "all"):
        show.dreams(store)
    if what in ("scratch", "all"):
        show.scratch(store, limit=args.limit)
    if what in ("calls", "all"):
        show.calls(store)


def main(argv=None):
    p = argparse.ArgumentParser(prog="rook-memory", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--store", default=DEFAULT_STORE, help=f"the folder the store lives in (default {DEFAULT_STORE})")
    p.add_argument("--knobs", default=None, help="the knobs file (default knobs.toml beside the package)")
    sub = p.add_subparsers(dest="command", required=True)
    t = sub.add_parser("take", help="take closed sessions in")
    t.add_argument("files", nargs="+")
    sub.add_parser("dream", help="run a dream pass now")
    a = sub.add_parser("ask", help="what a turn would be given")
    a.add_argument("question")
    a.add_argument("--peek", action="store_true", help="do not count this as reading what it returns")
    a.add_argument("--at", default=None, help="rank as if it were this time (ISO 8601), for recency")
    s = sub.add_parser("show", help="what is in memory")
    s.add_argument("what", nargs="?", default="all",
                   choices=["all", "sessions", "episodes", "beliefs", "scratch", "dreams", "calls"])
    s.add_argument("--session", type=int, default=None, help="episodes of this session only")
    s.add_argument("--limit", type=int, default=None, help="at most this many episodes, or the last this many scratch entries")
    sub.add_parser("reset", help="remove the store")
    args = p.parse_args(argv)

    folder = pathlib.Path(args.store).expanduser()
    if args.command == "reset":
        removed = [f for f in folder.glob("memory.sqlite3*") if f.is_file()]
        for f in removed:
            f.unlink()
        print(f"removed {', '.join(str(f) for f in removed) or 'nothing'}")
        return 0
    knobs = knobs_mod.load(args.knobs)
    store = Store(folder)
    try:
        if args.command == "take":
            cmd_take(store, knobs, args)
        elif args.command == "dream":
            dream.run(store, knobs, trigger="hand")
        elif args.command == "ask":
            cmd_ask(store, knobs, args)
        elif args.command == "show":
            cmd_show(store, knobs, args)
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
