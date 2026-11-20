"""Command line: python -m rook_memory [--store DIR] [--knobs FILE] COMMAND ..."""

import argparse
import json
import os
import pathlib
import sys

from . import check, compare, dream, importer, knobs as knobs_mod, mcp, pin, recall, show, standing, turn
from .store import Store, now, stamp


def main(argv=None):
    ap = argparse.ArgumentParser(prog="rook_memory", description="The house agent's memory prototype (round two).")
    ap.add_argument("--store", default="~/.rook-memory-proto", help="folder holding memory.sqlite3 (and the model)")
    ap.add_argument("--knobs", default=None, help="knobs file (default: knobs.toml beside the package)")
    ap.add_argument("--set", action="append", default=[], metavar="SECTION.KEY=VALUE",
                    help="override one knob for this run (repeatable), e.g. --set search.model=bge-base")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("take", help="take conversations in from an export (zip, folder or conversations.json)")
    p.add_argument("path")
    p.add_argument("--pick", nargs="*", default=[], help="only conversations whose name contains one of these")
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument("--list", action="store_true", help="list the conversations, take nothing")
    p.add_argument("--dream", action="store_true", help="dream over each one after taking it in (a replay)")

    p = sub.add_parser("play", help="play made-up conversations as live turns, dreaming at the end of each")
    p.add_argument("path")
    p.add_argument("--pick", nargs="*", default=[])
    p.add_argument("--no-dream", action="store_true")

    p = sub.add_parser("say", help="one live turn in a named session")
    p.add_argument("session")
    p.add_argument("text")
    p.add_argument("--as", dest="speaker", default=None, help="who says it (default: the person)")
    p.add_argument("--at", default=None, help="when (ISO time; default now)")
    p.add_argument("--voice", action="store_true")

    p = sub.add_parser("dream", help="dream over a session's record since its last dream")
    p.add_argument("session", nargs="?", help="session id or name")
    p.add_argument("--all", action="store_true", help="every session with something not yet dreamed over")
    p.add_argument("--trigger", default="hand")

    sub.add_parser("hook", help="for Claude Code's PreCompact hook: reads the hook's JSON on stdin and dreams that session")

    p = sub.add_parser("ask", help="what recall hands a turn for this question, and why (counts as no use)")
    p.add_argument("question")
    p.add_argument("--at", default=None)

    p = sub.add_parser("pin", help="pin from Bake's words (recorded as his message in the 'command line' session)")
    p.add_argument("statement")
    p.add_argument("--belief")
    p.add_argument("--about", default="world")
    p = sub.add_parser("unpin", help="lift a pin (his words, as for pin)")
    p.add_argument("words", nargs="?", default="no, don't pin that")
    p.add_argument("--belief")

    p = sub.add_parser("history", help="one belief's whole history")
    p.add_argument("belief")

    p = sub.add_parser("show", help="what is in memory")
    p.add_argument("what", nargs="?", default="all",
                   choices=["all", "sessions", "scratch", "record", "episodes", "beliefs", "dreams", "calls", "tools"])
    p.add_argument("--session", type=int)

    p = sub.add_parser("check", help="the made-up set's regression expectations, against what memory holds now")
    p.add_argument("expect", nargs="?", default=str(pathlib.Path(__file__).resolve().parent.parent / "madeup" / "expect.toml"))

    p = sub.add_parser("serve", help="(internal) the memory tools over MCP for one claude -p call")
    p.add_argument("--role", choices=["turn", "dream"], required=True)
    p.add_argument("--session", type=int)
    p.add_argument("--entry", type=int)
    p.add_argument("--at")

    p = sub.add_parser("tally", help="the checks over several stores (repeated plays): how often each fails")
    p.add_argument("stores", nargs="+")
    p.add_argument("--expect", default=str(pathlib.Path(__file__).resolve().parent.parent / "madeup" / "expect.toml"))

    p = sub.add_parser("repeat", help="play (or replay) the made-up set N times in parallel, one store each, then tally")
    p.add_argument("n", type=int)
    p.add_argument("--into", required=True, help="folder for the runs' stores (run-1, run-2, ...)")
    p.add_argument("--replay", action="store_true", help="take --dream instead of play")
    p.add_argument("--path", default=str(pathlib.Path(__file__).resolve().parent.parent / "madeup" / "conversations.json"))

    p = sub.add_parser("compare-models", help="recall questions against this store with each embedding model")
    p.add_argument("models", nargs="+", help="names of [models] tables, e.g. bge-small bge-base")
    p.add_argument("--questions", default=str(pathlib.Path(__file__).resolve().parent.parent / "madeup" / "questions.toml"))

    p = sub.add_parser("bench", help="load, search and embed timings and peak memory for one model (one JSON line)")
    p.add_argument("model")
    p.add_argument("--sizes", nargs="+", type=int, default=[500, 5000, 50000])
    p.add_argument("--folder", default="/tmp/rook-memory-bench")

    sub.add_parser("opposites", help="each pair of beliefs marked as opposites, standing with the knob off and on")

    sub.add_parser("cases", help="the three score cases (one against a mature belief, three times, a flip), with these knobs")

    sub.add_parser("reset", help="delete the store's memory (not the model)")

    a = ap.parse_args(argv)
    knobs = knobs_mod.load(a.knobs, a.set)
    for var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ.setdefault(var, str(knobs["search"]["threads"]))
    store_dir = pathlib.Path(a.store).expanduser()
    if a.cmd == "tally":
        return 0 if check.tally(a.stores, knobs, a.expect) else 1
    if a.cmd == "repeat":
        return repeat(a, knobs)
    if a.cmd == "bench":
        compare.bench(knobs, a.model, a.sizes, a.folder)
        return 0
    if a.cmd == "cases":
        print("\n".join(standing.cases(knobs)))
        return 0
    if a.cmd == "reset":
        for f in store_dir.glob("memory.sqlite3*"):
            f.unlink()
        print(f"memory in {store_dir} removed")
        return 0
    store = Store(store_dir)
    person = knobs["people"]["person"]

    if a.cmd == "serve":
        mcp.serve(store, knobs, a.role, a.session, a.entry, a.at or now())
    elif a.cmd == "take":
        convs = importer.pick(importer.conversations(a.path, knobs), a.pick, a.since, a.until)
        if a.list:
            for c in convs:
                print(f"{str(c['created_at'])[:10]}  {len(c['messages']):4} messages  {c['name']}  ({c['id']})")
            return 0
        for sid in importer.take(store, knobs, a.path, convs):
            if a.dream:
                dream.run(store, knobs, sid, trigger="hand")
    elif a.cmd == "play":
        turn.play(store, knobs, a.path, turn.made_up(a.path, knobs, a.pick), dream_after=not a.no_dream)
    elif a.cmd == "say":
        s = turn.session(store, a.session)
        answer, r = turn.say(store, knobs, s, a.text, a.speaker or person, a.at or now(), "voice" if a.voice else "chat")
        print(f"recalled: {', '.join(i.tag for i in r.items()) or 'nothing'}\n{answer}")
    elif a.cmd == "dream":
        if a.all:
            ids = [r["id"] for r in store.all("SELECT id FROM sessions ORDER BY started_at")]
        else:
            row = _session(store, a.session)
            ids = [row["id"]] if row else []
        for sid in ids:
            dream.run(store, knobs, sid, trigger=a.trigger)
    elif a.cmd == "hook":
        info = json.loads(sys.stdin.read() or "{}")
        row = store.one("SELECT * FROM sessions WHERE cc_session = ?", info.get("session_id"))
        if row:
            dream.run(store, knobs, row["id"], trigger="compaction")
    elif a.cmd == "ask":
        print(recall.explain(recall.recall(store, knobs, a.question, stamp(a.at or now()))))
    elif a.cmd in ("pin", "unpin"):
        s = turn.session(store, "command line")
        words = a.statement if a.cmd == "pin" else a.words
        entry = store.add_entry(s["id"], now(), person, "chat", words)
        if a.cmd == "pin":
            print(pin.pin(store, knobs, a.statement, entry, a.belief, a.about)[1])
        else:
            print(pin.unpin(store, knobs, entry, a.belief)[1])
    elif a.cmd == "history":
        show.history(store, knobs, int(a.belief.lstrip("bB")))
    elif a.cmd == "show":
        w = a.what
        if w in ("all", "sessions"):
            show.sessions(store)
        if w in ("all", "scratch"):
            show.entries(store, session=a.session, scratch_only=True)
        if w == "record":
            show.entries(store, session=a.session)
        if w in ("all", "episodes"):
            show.episodes(store, session=a.session)
        if w in ("all", "beliefs"):
            show.beliefs(store, knobs)
        if w in ("all", "dreams"):
            show.dreams(store)
        if w in ("all", "calls"):
            show.calls(store)
        if w == "tools":
            show.tools(store)
    elif a.cmd == "opposites":
        compare.opposites(store, knobs)
    elif a.cmd == "compare-models":
        compare.models(store, knobs, a.models, a.questions)
    elif a.cmd == "check":
        return 0 if check.run(store, knobs, a.expect) else 1
    return 0


def repeat(a, knobs):
    import subprocess
    into = pathlib.Path(a.into).expanduser()
    procs = []
    here = pathlib.Path(__file__).resolve().parent.parent
    for k in range(1, a.n + 1):
        d = into / f"run-{k}"
        d.mkdir(parents=True, exist_ok=True)
        argv = [sys.executable, "-u", "-m", "rook_memory", "--store", str(d), "--knobs", knobs["_path"]]
        for o in knobs["_overrides"]:
            argv += ["--set", o]
        argv += ["take", a.path, "--dream"] if a.replay else ["play", a.path]
        log = open(d / ("replay.log" if a.replay else "play.log"), "w")
        procs.append((subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, cwd=here), log))
        print(f"run-{k}: {' '.join(argv[3:])}")
    for p, log in procs:
        p.wait()
        log.close()
    return 0 if check.tally([into / f"run-{k}" for k in range(1, a.n + 1)], knobs,
                            str(here / "madeup" / "expect.toml")) else 1


def _session(store, ref):
    if ref is None:
        return store.one("SELECT * FROM sessions ORDER BY last_at DESC LIMIT 1")
    if str(ref).isdigit():
        return store.one("SELECT * FROM sessions WHERE id = ?", int(ref))
    return store.one("SELECT * FROM sessions WHERE name = ?", ref)


if __name__ == "__main__":
    sys.exit(main())
