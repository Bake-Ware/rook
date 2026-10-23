"""``rook hub``: build, deploy, inspect and roll back hub releases.

    rook hub release build [--ref HEAD] [--out DIR] [--url-base URL] [--repo DIR]
                           [--worker-bundle build|live|none|DIR] [--apk live|none|DIR]
                           [--apk-beta live|none|DIR] [--public-base URL]
    rook hub release verify MANIFEST [--pubkey B64]
    rook hub deploy MANIFEST [--services dashboard,mcp] [--test PATH]... [--yes]
    rook hub status [--json]
    rook hub history [-n 20]
    rook hub rollback [--to VERSION] [--services ...] [--yes]
    rook hub disarm [DEPLOY_ID]
    rook hub prune [--keep N] [--dry-run]
    rook hub units

All but ``release build/verify`` read the deploy config (``--config``,
``$ROOK_HUB_DEPLOY_CONFIG``, ``/etc/rook/hub-deploy.json``). See
docs/operations/hub-deploy.md.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import config as cfgmod
from . import deploy as dp
from . import manifest as mf


def _services(arg: str | None) -> list[str] | None:
    if not arg:
        return None
    return [s.strip() for s in arg.split(",") if s.strip()]


def _confirm(cfg, names: list[str] | None, yes: bool, what: str) -> None:
    targets = cfg.ordered(names) if names else cfg.ordered()
    disruptive = [s.name for s in targets if s.disruptive]
    if not disruptive or yes:
        return
    msg = (f"{what} restarts {', '.join(disruptive)}, which disconnects its clients "
           "(for the MCP server: every connected agent).")
    if not sys.stdin.isatty():
        raise dp.DeployError(msg + " Pass --yes to proceed, or --services to leave it out.")
    ans = input(msg + " Continue? [y/N] ").strip().lower()
    if ans not in ("y", "yes"):
        raise dp.DeployError("aborted")


def _activation_kw(a) -> dict:
    kw = {"restart": not a.no_restart, "parallel": a.parallel,
          "auto_rollback": not a.no_auto_rollback, "backup_dbs": not a.no_db_backup}
    if a.deadman_minutes is not None:
        kw["deadman_minutes"] = a.deadman_minutes
    return kw


def _print_status(st: dict) -> None:
    print(f"config  {st['config']}  (mode {st['mode']}, root {st['root']})")
    print("services:")
    for name, e in st["services"].items():
        line = f"  {name:10s} selected={e['selected'] or '-'}"
        if "effective" in e:
            line += f" effective={e['effective'] or '-'}"
        if "active" in e:
            line += f" [{e['active']}]"
        line += f" previous={e['previous'] or '-'}"
        print(line)
        if e.get("effective") and e.get("selected") and e["effective"] != e["selected"]:
            print(f"    WARNING: systemd applies {e['effective']}, not the selected release")
        for p in e.get("strays") or []:
            print(f"    WARNING: stray release drop-in {p} (deploy refuses until adopted/removed)")
    print("releases:")
    for r in st["releases"]:
        use = f"  <- {', '.join(r['in_use_by'])}" if r["in_use_by"] else ""
        print(f"  {r['version']:28s} {(r['commit'] or '')[:12]:12s} {r['built_at'] or '':25s}{use}")
    if st["armed"]:
        print(f"dead-man ARMED for: {', '.join(st['armed'])}")
    prev = {n: e["previous"] for n, e in st["services"].items() if e["previous"]}
    if prev:
        print(f"rollback: rook hub rollback --config {st['config']}   "
              f"({', '.join(f'{n} -> {v}' for n, v in prev.items())})")


def _history_line(ev: dict) -> str:
    import datetime
    ts = datetime.datetime.fromtimestamp(ev.get("ts", 0)).strftime("%Y-%m-%d %H:%M:%S")
    ch = ", ".join(f"{n} {c.get('from') or '-'}->{c.get('to') or '-'}"
                   for n, c in (ev.get("services") or {}).items())
    extra = f" reason={ev['reason']}" if ev.get("reason") else ""
    extra += f" error={ev['error']}" if ev.get("error") else ""
    return f"{ts}  {ev.get('event', '?'):8s} {ev.get('result', ''):8s} {ch}{extra}"


def _live_feed(config: str | None):
    """(release tree the live feed service runs, its environment), or None
    when there is no deploy config or no running release."""
    from . import feed as fd
    from .selector import Selector, systemctl_argv
    try:
        cfg = cfgmod.load(config)
    except cfgmod.ConfigError:
        return None
    svc = cfg.feed_svc()
    cur = Selector(cfg).current(svc) if svc else None
    if not cur or not cfg.release_dir(cur).is_dir():
        return None
    argv = (systemctl_argv(cfg, "show", "-p", "Environment", "--value", svc.unit)
            if cfg.mode == "systemd" and svc.unit else None)
    return cfg.release_dir(cur), fd.service_env(argv)


def _feed_sources(a) -> dict:
    """build_release(worker=, apk=, apk_beta=, public_base=) from the flags."""
    from . import feed as fd
    wants_live = ("live" in (a.worker_bundle, a.apk, a.apk_beta)
                  or not (a.worker_bundle and a.apk and a.apk_beta))
    live = _live_feed(a.config) if wants_live else None

    def need_live(flag):
        if live is None:
            raise cfgmod.ConfigError(f"{flag} live: no deploy config with a running release "
                                     "(pass --config, or a directory)")
        return live

    out = {"worker": None, "apk": None, "apk_beta": None}
    worker = a.worker_bundle or ("live" if live and "worker" in fd.tree_kinds(live[0]) else "build")
    if worker == "build":
        out["worker"] = "build"
    elif worker == "live":
        out["worker"] = need_live("--worker-bundle")[0] / fd.REMOTE
    elif worker != "none":
        out["worker"] = Path(worker)
    apk = a.apk or ("live" if live and "apk" in fd.tree_kinds(live[0]) else "none")
    if apk == "live":
        tree = need_live("--apk")[0]
        if "apk" not in fd.tree_kinds(tree):
            raise cfgmod.ConfigError(f"--apk live: {tree.name} serves no APK")
        out["apk"] = tree / fd.REMOTE
    elif apk != "none":
        out["apk"] = Path(apk)
    beta = a.apk_beta or "live"
    if beta == "live":
        found = None
        if live is not None:
            tree, env = live
            gate = (env.get("ROOK_BETA_APK_SHA256") or "").strip()
            upd = fd.updates_dir(env)
            for d in ([upd / "apk-beta"] if upd else []) + [tree / fd.BETA]:
                m = fd._read_json(d / fd.APK_MANIFEST)
                if gate and isinstance(m, dict) and m.get("sha256") == gate and (d / fd.APK).is_file():
                    found = d
                    break
        if found is None and a.apk_beta == "live":
            raise cfgmod.ConfigError("--apk-beta live: the live hub approves no beta APK")
        out["apk_beta"] = found
    elif beta != "none":
        out["apk_beta"] = Path(beta)
    base = a.public_base or os.environ.get("ROOK_PUBLIC_BASE", "")
    if not base and live is not None:
        m = fd._read_json(live[0] / fd.REMOTE / fd.PYZ_MANIFEST) or {}
        url = str(m.get("url") or "")
        base = url.rsplit("/", 1)[0] if "/" in url else ""
    out["public_base"] = base
    return out


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="rook hub", description="Hub release deploys")
    ap.add_argument("--config", help="deploy config JSON (default $ROOK_HUB_DEPLOY_CONFIG "
                                     "or /etc/rook/hub-deploy.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    rel = sub.add_parser("release", help="build or verify a signed release")
    rsub = rel.add_subparsers(dest="rcmd", required=True)
    b = rsub.add_parser("build", help="git archive a commit + write its signed manifest")
    b.add_argument("--ref", default="HEAD")
    b.add_argument("--repo", default=".", help="git checkout to archive (default .)")
    b.add_argument("--out", default="dist/hub", help="output dir (default dist/hub)")
    b.add_argument("--url-base", default="", help="public URL the tarball will be served "
                                                  "from; signed into the manifest")
    b.add_argument("--worker-bundle", default="", metavar="build|live|none|DIR",
                   help="worker bundle the release serves at /band-worker.pyz: the one the "
                        "live hub serves (default when a deploy config with a running release "
                        "is found, so a hub deploy never changes what new workers get), build "
                        "it from the commit (the default otherwise), none, or a directory with "
                        "a signed band-worker.pyz + band-worker.json")
    b.add_argument("--apk", default="", metavar="live|none|DIR",
                   help="Android app the release serves at /apk: the live hub's (default when "
                        "a deploy config is found), none, or a directory with rook-worker.apk + "
                        "rook-worker-apk.json")
    b.add_argument("--apk-beta", default="", metavar="live|none|DIR",
                   help="beta-channel app (/apk?channel=beta): the live hub's approved beta "
                        "(default when a deploy config is found), none, or a directory")
    b.add_argument("--public-base", default="",
                   help="hub URL signed into a built worker bundle's manifest (default "
                        "$ROOK_PUBLIC_BASE, else the live bundle's)")
    v = rsub.add_parser("verify", help="check a manifest's signature (and artifact if local)")
    v.add_argument("manifest")
    v.add_argument("--pubkey")

    def activation_opts(p):
        p.add_argument("--services", help="comma list (default: all configured)")
        p.add_argument("--yes", action="store_true", help="don't ask before restarting "
                                                          "disruptive services (the MCP)")
        p.add_argument("--no-restart", action="store_true",
                       help="switch the selector only; restart later yourself")
        p.add_argument("--parallel", action="store_true",
                       help="restart all services, then verify (default: one at a time "
                            "in order, verifying each before the next)")
        p.add_argument("--deadman-minutes", type=float,
                       help="auto-rollback timer (default from config, 10; 0 disables)")
        p.add_argument("--no-auto-rollback", action="store_true",
                       help="on failed verification leave things as they are "
                            "(the dead-man timer still fires)")
        p.add_argument("--no-db-backup", action="store_true")
        p.add_argument("--adopt-strays", action="store_true",
                       help="move other release-selecting drop-ins into the deploy backup")

    d = sub.add_parser("deploy", help="deploy a signed release")
    d.add_argument("manifest", help="manifest path or URL")
    d.add_argument("--test", action="append", default=[],
                   help="pytest path inside the release to run as preflight (repeatable)")
    d.add_argument("--pubkey", help="trusted update public key (default: this hub's)")
    d.add_argument("--allow-downgrade", action="store_true")
    d.add_argument("--skip-preflight", action="store_true")
    d.add_argument("--no-carry-feed", action="store_true",
                   help="don't carry the worker bundle / APKs the release lacks from the "
                        "running release")
    d.add_argument("--skip-feed-check", action="store_true",
                   help="don't check the APK gates before, or what /band-worker.pyz and /apk "
                        "serve after (a dark feed then goes unnoticed)")
    activation_opts(d)

    r = sub.add_parser("rollback", help="switch services back to their previous release")
    r.add_argument("--to", help="release version (default: each service's previous)")
    activation_opts(r)

    s = sub.add_parser("status", help="current/previous release per service")
    s.add_argument("--json", action="store_true")
    h = sub.add_parser("history", help="deploy and rollback events")
    h.add_argument("-n", type=int, default=20)
    h.add_argument("--json", action="store_true")
    ds = sub.add_parser("disarm", help="disarm a pending dead-man rollback")
    ds.add_argument("deploy_id", nargs="?")
    p = sub.add_parser("prune", help="delete old releases, keeping N and anything in use")
    p.add_argument("--keep", type=int)
    p.add_argument("--keep-deploys", type=int, default=20)
    p.add_argument("--dry-run", action="store_true")
    sub.add_parser("units", help="print generic systemd units for this config")
    # Listed for --help only: main() hands `serve` to rook.hub.serve untouched.
    sub.add_parser("serve", add_help=False,
                   help="run the hub: dashboard + MCP server in one process "
                        "(rook hub serve --help)")
    return ap


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["serve"]:
        from ..hub.serve import main as serve_main
        serve_main(argv[1:])
        return 0
    a = build_parser().parse_args(argv)
    try:
        if a.cmd == "release":
            if a.rcmd == "build":
                src = _feed_sources(a)
                tb, mp, m = mf.build_release(Path(a.repo), Path(a.out), a.ref, a.url_base,
                                             log=print, **src)
                print(f"built {m['version']} (commit {m['commit'][:12]})\n  {tb}\n  {mp}")
                return 0
            m = mf.verify(mf.load_manifest(a.manifest), a.pubkey)
            print(f"signature ok: {m['version']} commit {m['commit']}")
            src = mf.artifact_source(m, a.manifest)
            ok = True
            if Path(src).is_file():
                ok = mf.sha256_file(Path(src)) == m["sha256"]
                print(f"artifact {src}: {'sha256 ok' if ok else 'SHA256 MISMATCH'}")
            from . import feed as fd
            for e in m.get("feed") or []:
                b = fd.blob_source(m, a.manifest, e["sha256"])
                if Path(b).is_file():
                    good = mf.sha256_file(Path(b)) == e["sha256"]
                    ok = ok and good
                    print(f"feed {e['path']}: {'sha256 ok' if good else 'SHA256 MISMATCH'}")
            return 0 if ok else 1

        cfg = cfgmod.load(a.config)
        if a.cmd == "deploy":
            names = _services(a.services)
            _confirm(cfg, names, a.yes or a.no_restart, "this deploy")
            dp.deploy(cfg, a.manifest, services=names, tests=a.test, pubkey=a.pubkey,
                      allow_downgrade=a.allow_downgrade, skip_preflight=a.skip_preflight,
                      adopt_strays=a.adopt_strays, carry_feed=not a.no_carry_feed,
                      check_feed=not a.skip_feed_check, **_activation_kw(a))
        elif a.cmd == "rollback":
            names = _services(a.services)
            _confirm(cfg, names, a.yes or a.no_restart, "this rollback")
            dp.rollback(cfg, to=a.to, services=names, adopt_strays=a.adopt_strays,
                        **_activation_kw(a))
        elif a.cmd == "status":
            st = dp.status(cfg)
            if a.json:
                print(json.dumps(st, indent=2))
            else:
                _print_status(st)
        elif a.cmd == "history":
            hist = dp.read_history(cfg)[-a.n:]
            for ev in hist:
                print(json.dumps(ev) if a.json else _history_line(ev))
        elif a.cmd == "disarm":
            ids = [a.deploy_id] if a.deploy_id else dp.armed_deploys(cfg)
            if not ids:
                print("nothing armed")
            for i in ids:
                dp.disarm_deadman(cfg, cfg.state / "deploys" / i)
        elif a.cmd == "prune":
            removed = dp.prune(cfg, a.keep, a.keep_deploys, a.dry_run)
            print(f"{'would remove' if a.dry_run else 'removed'} {len(removed)} release(s)")
        elif a.cmd == "units":
            print(dp.units_text(cfg))
        return 0
    except (dp.DeployError, mf.ManifestError, cfgmod.ConfigError) as e:
        print(f"rook hub: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
