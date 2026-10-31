"""Ready-made job: security scan (0.4's routine.security_scan, cut to what
0.5 has). Reads the journal since the last scan and tells the owners when
refused calls pile up. vars: threshold (default 10; 0: never tell)."""
from collections import Counter
import time


def main(job):
    since = job.last or time.time() - 300
    entries = job.call("journal.search", {"since": since, "limit": 500},
                       why="look for refused and held calls since the last scan")["entries"]
    denied = [e for e in entries if e.get("decision") == "deny"]
    held = [e for e in entries if e.get("decision") == "held"]
    out = {"since": since, "denied": len(denied), "held": len(held)}
    threshold = int(job.vars.get("threshold", 10))
    if threshold and len(denied) >= threshold:
        who = Counter(e.get("agent") or e.get("person") or "?" for e in denied).most_common(3)
        caps = Counter(e.get("cap") for e in denied).most_common(3)
        job.notify(f"{len(denied)} calls were refused in the last {int((time.time() - since) // 60) or 1} min. "
                   f"Most by {', '.join(f'{w} ({n})' for w, n in who)}; "
                   f"most for {', '.join(f'{c} ({n})' for c, n in caps)}.",
                   title="Security scan")
        out["told"] = True
    return out
