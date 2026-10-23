"""Clean-room runs from the command line.

    python3 -m tools.cleanroom list
    python3 -m tools.cleanroom run --hardware bachelor --model claude-subscription --trust balanced
    python3 -m tools.cleanroom sweep          # destroy leftover clean-room machines

See tools/cleanroom/README.md.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import traceback

from . import scenarios
from .report import Run

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SITE = os.path.expanduser("~/.config/rook-cleanroom/site.json")


def load_matrix() -> dict:
    with open(os.path.join(HERE, "matrix.json")) as f:
        return json.load(f)


def load_site(path: str) -> dict:
    with open(os.path.expanduser(path)) as f:
        return json.load(f)


def validate(matrix: dict) -> list[str]:
    """Problems with the matrix (empty when it is usable)."""
    errs = []
    ids = [s["id"] for s in matrix.get("scenarios", [])]
    known = {name for name, _ in scenarios.ORDER}
    for sid in ids:
        if sid not in known:
            errs.append(f"scenario {sid} has no implementation")
    for name, hw in matrix.get("hardware", {}).items():
        if hw.get("planned"):
            continue
        ms = hw.get("machines") or []
        if sum(1 for m in ms if m.get("hub")) != 1:
            errs.append(f"hardware {name}: exactly one machine must host the hub")
        if not any(m.get("worker") for m in ms):
            errs.append(f"hardware {name}: no worker machine")
        for m in ms:
            for k in ("role", "memory_mb", "cores"):
                if k not in m:
                    errs.append(f"hardware {name}: machine without {k}")
    for name, mp in matrix.get("models", {}).items():
        ha = mp.get("house_agent") or {}
        if ha.get("backend") not in ("claude-code", "openai", "codex", "opencode"):
            errs.append(f"model {name}: unknown house agent backend {ha.get('backend')}")
        lm = mp.get("local_model") or {}
        if lm.get("inherit") and lm["inherit"] not in matrix["models"]:
            errs.append(f"model {name}: inherits unknown {lm['inherit']}")
    for name, t in matrix.get("trust", {}).items():
        if t.get("default_level") not in ("ship-it", "balanced", "guarded", "sacred"):
            errs.append(f"trust {name}: unknown level {t.get('default_level')}")
    return errs


def cmd_list(args):
    m = load_matrix()
    for kind in ("hardware", "models", "trust"):
        print(kind + ":")
        for k, v in m[kind].items():
            flag = " (planned)" if v.get("planned") else ""
            print(f"  {k}{flag}: {v.get('description', '')}")
    print("scenarios:")
    for s in m["scenarios"]:
        print(f"  {s['id']}: {s['title']}")
    errs = validate(m)
    if errs:
        print("\nmatrix problems:\n  " + "\n  ".join(errs))
        return 1
    return 0


def resolve_ref(ref: str) -> str:
    out = subprocess.run(["git", "-C", HERE, "rev-parse", ref], capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 else ref


def cmd_run(args):
    matrix, site = load_matrix(), load_site(args.site)
    errs = validate(matrix)
    if errs:
        print("matrix problems:\n  " + "\n  ".join(errs), file=sys.stderr)
        return 2
    hw = matrix["hardware"][args.hardware]
    if hw.get("planned"):
        print(f"hardware profile {args.hardware} is planned, not runnable yet", file=sys.stderr)
        return 2
    ref = resolve_ref(args.ref)
    run_id = time.strftime("%Y%m%d-%H%M%S") + f"-{args.hardware}-{args.model}-{args.trust}"
    reports = os.path.expanduser(args.reports or site.get("reports_dir", "~/rook-cleanroom-reports"))
    run_dir = os.path.join(reports, run_id)
    os.makedirs(run_dir, exist_ok=True)
    site["_run_dir"] = run_dir
    site["_only"] = set(args.only.split(",")) if args.only else None
    if args.apk:
        site["apk"] = args.apk
    logf = open(os.path.join(run_dir, "run.log"), "a")

    def log(msg):
        line = time.strftime("%H:%M:%S ") + str(msg)
        print(line, flush=True)
        logf.write(line + "\n")
        logf.flush()

    run = Run(run_id=run_id, hardware=args.hardware, model=args.model, trust=args.trust, rook_ref=ref)
    ctx = scenarios.Ctx(run, site, matrix, ref=ref, repo=args.repo, log=log)
    log(f"[run] {run_id} rook {ref}")
    try:
        for name, fn in scenarios.ORDER:
            if site["_only"] and name not in site["_only"] and name not in ("onboard-hub", "pair-laptop"):
                continue
            if name == "pair-phone" and not hw.get("phone"):
                continue
            log(f"[scenario] {name}")
            try:
                fn(ctx)
            except Exception as e:  # noqa: BLE001 - a scenario's crash is a result
                log(f"[scenario] {name} crashed: {e}")
                logf.write(traceback.format_exc())
                with run.step(name, "scenario crashed") as s:
                    s.fail(f"{type(e).__name__}: {e}")
                if name in ("onboard-hub",):
                    break
            for st in run.steps:
                if st.scenario == name:
                    log(f"  {st.status:4} {st.name} ({st.seconds:.0f} s) {'; '.join(st.notes)[:200]} {st.error[:200]}")
        if ctx.hub:
            tok = scenarios.usage_tokens(ctx)
            if tok:
                run.env["agent_host_usage_24h"] = tok
    finally:
        run.finished = time.time()
        j, md = run.write(run_dir)
        log(f"[run] report: {md}")
        try:
            ctx.cleanup(keep=args.keep)
        except Exception as e:  # noqa: BLE001
            log(f"[run] cleanup failed: {e}")
    t = run.totals()
    return 0 if t["failed"] == 0 else 1


def cmd_sweep(args):
    from .providers import ProxmoxProvider
    site = load_site(args.site)
    ProxmoxProvider(site["proxmox"]).sweep()
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="python3 -m tools.cleanroom", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--site", default=DEFAULT_SITE, help="site config (hypervisor, emulator, secrets)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="show the matrix")
    r = sub.add_parser("run", help="one run of one matrix cell")
    r.add_argument("--hardware", default="bachelor")
    r.add_argument("--model", default="claude-subscription")
    r.add_argument("--trust", default="balanced")
    r.add_argument("--ref", default="origin/master", help="Rook git ref to install (resolved to a sha)")
    r.add_argument("--repo", default="Bake-Ware/rook", help="GitHub repo the installer and code come from")
    r.add_argument("--only", help="comma-separated scenarios (onboarding always runs)")
    r.add_argument("--keep", action="store_true", help="leave the machines up afterwards")
    r.add_argument("--apk", help="the APK to install on the phone (default: the site's)")
    r.add_argument("--reports", help="reports directory (default: site reports_dir)")
    sub.add_parser("sweep", help="destroy leftover clean-room machines")
    args = p.parse_args(argv)
    return {"list": cmd_list, "run": cmd_run, "sweep": cmd_sweep}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
