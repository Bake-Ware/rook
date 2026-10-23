"""The clean-room scenarios, end to end and without a person.

Each scenario drives the system the way a new user would and records, per
step, what a real person would have had to do by hand ("human touches"):
pasting an install command, clicking a dashboard button, typing a pairing
code, approving a hold. The person's own requests to the house agent are
not touches; anything the system makes them do on top is.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
import threading
import time

from . import android, models, toy_seed
from .hub import Hub, HubError
from .providers import EmulatorProvider, Machine, ProxmoxProvider
from .report import Run

INSTALLER = "https://raw.githubusercontent.com/{repo}/{ref}/scripts/install-hub.sh"
PHONE_NAME = "phone"


class Ctx:
    """Everything one run needs: the profile, the machines, the hub."""

    def __init__(self, run: Run, site: dict, matrix: dict, *, ref: str, repo: str, log=print,
                 release: str = ""):
        self.run, self.site, self.matrix, self.ref, self.repo, self.log = run, site, matrix, ref, repo, log
        # release: install a published release (the installer's default path,
        # "latest" or a tag) instead of git at ref.
        self.release = release
        self.hw = matrix["hardware"][run.hardware]
        self.model = dict(matrix["models"][run.model])
        lm = self.model.get("local_model") or {}
        if lm.get("inherit"):
            self.model["local_model"] = matrix["models"][lm["inherit"]]["local_model"]
        self.trust = matrix["trust"][run.trust]
        self.vms = ProxmoxProvider(site["proxmox"], log=log)
        self.emu = EmulatorProvider(site["emulator"], log=log) if self.hw.get("phone") else None
        self.machines: dict[str, Machine] = {}
        self.specs = {m["role"]: m for m in self.hw["machines"]}
        self.hub: Hub | None = None
        self.secrets_env: dict = {}
        self.code = ""
        self.code_expires = 0.0
        self.local_base = ""

    # -- helpers ---------------------------------------------------------------
    def secret(self, name: str) -> str:
        spec = (self.site.get("secrets") or {}).get(name)
        if not spec:
            return ""
        if "file" in spec:
            return open(os.path.expanduser(spec["file"])).read().strip()
        if "env" in spec:
            return os.environ.get(spec["env"], "")
        if "cmd" in spec:
            return subprocess.run(spec["cmd"], capture_output=True, text=True, check=True).stdout.strip()
        return ""

    @property
    def hub_machine(self) -> Machine:
        role = next(m["role"] for m in self.hw["machines"] if m.get("hub"))
        return self.machines[role]

    def worker_machines(self) -> list[Machine]:
        return [self.machines[m["role"]] for m in self.hw["machines"] if m.get("worker")]

    @property
    def agent_host(self) -> Machine:
        return self.worker_machines()[0]

    def wants(self, scenario: str) -> bool:
        only = self.site.get("_only")
        return not only or scenario in only

    def cleanup(self, keep=False):
        if keep:
            self.log("[run] --keep: leaving machines up: " +
                     ", ".join(f"{m.name}={m.host}" for m in self.machines.values()))
            return
        if self.emu:
            self.emu.destroy()
        for m in self.machines.values():
            self.vms.destroy(m)


# ---------------------------------------------------------------------------
# 1. Install the hub from zero
# ---------------------------------------------------------------------------

def onboard_hub(ctx: Ctx):
    run = ctx.run
    with run.step("onboard-hub", "create fresh machines") as s:
        for spec in ctx.hw["machines"]:
            m = ctx.vms.create(spec["role"], memory_mb=spec["memory_mb"], cores=spec["cores"],
                               disk_gb=spec.get("disk_gb", 20))
            ctx.machines[spec["role"]] = m
            run.machines.append({"role": spec["role"], **m.meta, "name": m.name})
        s.note(", ".join(f"{r}: {m.meta['memory_mb']} MB/{m.meta['cores']} cores" for r, m in ctx.machines.items()))
    if run.steps[-1].status != "pass":
        raise RuntimeError("no machines")
    hubm = ctx.hub_machine
    with run.step("onboard-hub", "one-command hub installer") as s:
        s.touch("paste the installer one-liner into a terminal on the hub machine")
        if ctx.release:
            # What the README tells a new user: master's installer, which
            # installs the latest release (or the tag given).
            url = INSTALLER.format(repo=ctx.repo, ref="master" if ctx.release == "latest" else ctx.release)
            flags = "" if ctx.release == "latest" else f" --version {shlex.quote(ctx.release)}"
            r = hubm.sh(f"curl -fsSL {url} | bash -s -- --yes{flags} 2>&1", timeout=1800, check=False)
        else:
            url = INSTALLER.format(repo=ctx.repo, ref=ctx.ref)
            r = hubm.sh(f"curl -fsSL {url} | bash -s -- --yes --git-ref {ctx.ref} 2>&1", timeout=1800, check=False)
        out = r.stdout
        open(os.path.join(ctx.site["_run_dir"], "hub-install.log"), "w").write(out)
        if r.returncode != 0:
            s.fail(f"installer exit {r.returncode}: {out[-600:]}")
        pw = re.search(r"Password\s+(\S+)", out)
        if not pw:
            s.fail("installer did not print a dashboard password")
        if "WARNING" in out:
            s.note("installer warnings: " + "; ".join(sorted({l.split('WARNING:', 1)[1].strip()[:90] for l in out.splitlines() if 'WARNING:' in l}))[:300])
        env = hubm.sh("sudo cat /var/lib/rook/secrets.env").stdout
        ctx.secrets_env = dict(l.split("=", 1) for l in env.split() if "=" in l)
        ctx.hub = Hub(f"http://{hubm.host}:7005", pw.group(1), ctx.secrets_env.get("ROOK_MCP_STATIC_TOKEN", ""))
        deadline = time.time() + 60
        while not ctx.hub.health():
            if time.time() > deadline:
                s.fail("hub /health never answered")
            time.sleep(2)
        m = re.search(r"Rook (\S+) \(build (\S+)\) installed", out)
        if m:
            ctx.run.env["rook_installed"] = f"{m.group(1)} ({m.group(2)})"
        s.note(f"installed in {time.time() - s.s.started:.0f} s"
               + (f": Rook {m.group(1)} ({m.group(2)})" if m else ""))
    with run.step("onboard-hub", "dashboard sign-in") as s:
        s.touch("open the dashboard and type the printed password")
        ctx.hub.login()


# ---------------------------------------------------------------------------
# 2. Pair the laptop (and any other worker machine)
# ---------------------------------------------------------------------------

def pair_workers(ctx: Ctx):
    run, hub = ctx.run, ctx.hub
    for m in ctx.worker_machines():
        with run.step("pair-laptop", f"pair {m.name}") as s:
            if not ctx.code or time.time() > ctx.code_expires - 30:
                s.touch("Install page: click 'Get a pairing code'")
                p = hub.pairing_code()
                ctx.code, ctx.code_expires = p["code"], float(p.get("expires") or time.time() + 300)
            s.touch(f"paste the worker one-liner on {m.name}")
            r = m.sh(f"curl -fsSL 'http://{ctx.hub_machine.host}:7005/worker?band={ctx.code}' | bash 2>&1",
                     timeout=1200, check=False)
            if r.returncode != 0:
                s.fail(f"worker installer exit {r.returncode}: {r.stdout[-500:]}")
            hub.wait_worker(m.name, timeout=180)
            s.note(f"online after {time.time() - s.s.started:.0f} s")
    ctx.run.env.setdefault("first_worker_call_s", round(time.time() - ctx.run.started, 1))
    with run.step("pair-laptop", "first call on the new worker") as s:
        m = ctx.worker_machines()[0]
        r = hub.band_call("shell.exec", {"argv": ["uname", "-sr"], "timeout": 20}, worker=m.name)
        if not r.get("ok"):
            s.fail(json.dumps(r)[:300])
        s.note("uname: " + str((r.get("result") or {}).get("stdout", "")).strip())


# ---------------------------------------------------------------------------
# 3. Install the APK and pair the phone
# ---------------------------------------------------------------------------

def pair_phone(ctx: Ctx):
    run, hub, emu = ctx.run, ctx.hub, ctx.emu
    if emu is None:
        return
    with run.step("pair-phone", "boot a wiped phone") as s:
        emu.create()
        for k in ("window_animation_scale", "transition_animation_scale", "animator_duration_scale"):
            emu.adb("shell", "settings", "put", "global", k, "0", check=False)
        emu.adb("shell", "settings", "put", "global", "hide_error_dialogs", "1", check=False)
    with run.step("pair-phone", "get and install the APK") as s:
        # The way a phone gets it: open http://<hub>/apk, no sign-in.
        st, _, man = hub._req("GET", "/apk.json", timeout=30)
        apk_path = ""
        if st == 200:
            meta = json.loads(man)
            st, _, body = hub._req("GET", "/apk", timeout=600)
            import hashlib
            if st == 200 and hashlib.sha256(body).hexdigest() == meta.get("sha256"):
                fd, apk_path = tempfile.mkstemp(suffix=".apk", prefix="cleanroom-hub-")
                with os.fdopen(fd, "wb") as f:
                    f.write(body)
                s.note(f"installed the hub's own APK from /apk: {meta.get('version_name')} "
                       f"({meta.get('version_code')}, sha256 {meta['sha256'][:12]}), no sign-in")
                run.env["apk"] = f"hub /apk {meta.get('version_name')} ({meta['sha256'][:12]})"
                run.env["apk_source"] = "hub"
            else:
                s.note(f"/apk.json answered but /apk gave {st} or a different file")
        else:
            s.note(f"the hub has no public APK (/apk.json -> {st})")
        if not apk_path:
            if ctx.site.get("require_hub_apk"):
                s.fail("the fresh hub serves no APK, and this run requires the hub's own")
            s.note("sideloaded the site's APK")
            run.findings.append("A fresh hub has no APK at /apk and no release carries one: a new user has no way to get the app.")
            apk_path = os.path.expanduser(ctx.site["apk"])
            run.env["apk"] = os.path.basename(apk_path)
            run.env["apk_source"] = "site"
        s.touch("open the hub's /apk link on the phone and allow installing it")
        emu.adb("install", "-r", apk_path, timeout=300)
        ver = emu.adb("shell", "dumpsys", "package", android.PACKAGE).stdout
        m = re.search(r"versionName=(\S+)", ver)
        s.note(f"APK {m.group(1) if m else '?'}")
    with run.step("pair-phone", "pair with a pairing code") as s:
        s.touch("open the app")
        android.launch(emu)
        allow = None
        try:
            allow = android.wait_for(emu, rid="permission_allow_button", timeout=15)
        except TimeoutError:
            pass
        if allow:
            s.touch("allow notifications")
            android.tap(emu, allow)
        s.touch("open Settings")
        android.tap(emu, android.wait_for(emu, rid="btnSettings", timeout=30))
        s.touch("open the Band tab")
        android.tap(emu, android.wait_for(emu, text="Band"))
        server = f"http://{ctx.hub_machine.host}:7005"
        s.touch("type the hub address")
        android.type_into(emu, android.wait_for(emu, rid="accountServer"), server)
        if time.time() > getattr(ctx, "code_expires", 0) - 30:
            s.touch("Install page: click 'Get a pairing code'")
            p = hub.pairing_code()
            ctx.code, ctx.code_expires = p["code"], float(p.get("expires") or time.time() + 300)
        s.touch("type the pairing code")
        android.type_into(emu, android.wait_for(emu, rid="pairingCode"), ctx.code)
        s.touch("tap 'Fetch band with pairing code'")
        android.tap(emu, android.wait_for(emu, rid="btnPair"))
        time.sleep(4)
        nodes = android.dump(emu)
        status = android.find(nodes, rid="status")
        status_text = status.text if status else ""
        choice = android.find(nodes, contains="rook-band") or android.find(nodes, rid="select_dialog_listview")
        if choice and "HTTPS" not in status_text:
            s.touch("choose the band")
            android.tap(emu, choice)
            ok = android.find(android.dump(emu), text="OK")
            if ok:
                android.tap(emu, ok)
            s.touch("set a worker name and tap Start worker")
            android.type_into(emu, android.scroll_to(emu, rid="name"), PHONE_NAME)
            android.tap(emu, android.scroll_to(emu, rid="btnStart"))
            hub.wait_worker(PHONE_NAME, timeout=120)
        else:
            s.fail(f"pairing refused: {status_text!r}")
    if run.steps[-1].status == "fail":
        run.findings.append("The APK refuses a plain-HTTP hub address ('Enter the HTTPS address of your Rook "
                            "server'), but the one-command installer serves LAN hubs over HTTP: pairing by code "
                            "is impossible on a bachelor/LAN install.")
        with run.step("pair-phone", "fallback: hub address + band key by hand") as s:
            s.touch("find the band key on the hub machine (sudo cat /var/lib/rook/secrets.env)")
            s.touch("type the relay address host:7474")
            android.type_into(emu, android.scroll_to(emu, rid="hub"), f"{ctx.hub_machine.host}:7474")
            s.touch("type the 40-character band key")
            android.type_into(emu, android.scroll_to(emu, rid="psk"), ctx.secrets_env["ROOK_BAND_PSK"])
            s.touch("type a worker name")
            android.type_into(emu, android.scroll_to(emu, rid="name"), PHONE_NAME)
            s.touch("tap Start worker")
            android.tap(emu, android.scroll_to(emu, rid="btnStart"))
            w = hub.wait_worker(PHONE_NAME, timeout=120)
            s.note(f"phone online ({(w.get('app_release') or {}).get('version', '?')})")
    with run.step("pair-phone", "a call on the phone") as s:
        if not hub.worker(PHONE_NAME):
            s.skip("phone not on the band")
        r = hub.band_call("caps.describe", {"prefix": "device."}, worker=PHONE_NAME)
        if not r.get("ok"):
            s.fail(json.dumps(r)[:300])
        s.note(f"{len(r.get('result') or [])} device.* caps")


# ---------------------------------------------------------------------------
# 4. The model profile
# ---------------------------------------------------------------------------

def model_setup(ctx: Ctx):
    run, hub, host = ctx.run, ctx.hub, ctx.agent_host
    model = ctx.model
    ha = model["house_agent"]
    if ha["backend"] == "claude-code" or (model.get("sessions") or {}).get("harness") == "claude":
        with run.step("model-setup", "Claude Code on the agent host") as s:
            token = ctx.secret("claude_oauth_token")
            if not token:
                s.skip("no claude_oauth_token in the site config")
            s.touch("install Claude Code (one command)")
            s.touch("sign in to Claude in a browser")
            out = models.install_claude_code(host, token)
            if "ready" not in out.lower():
                s.fail(out[-400:])
            s.note(out.splitlines()[0])
            st = hub.band_call("agenthost.status", {}, worker=host.name)
            harn = ((st.get("result") or {}).get("harnesses") or {}).get("claude") or {}
            if not harn.get("logged_in"):
                s.fail(f"the worker does not see a signed-in Claude Code: {json.dumps(st)[:300]}")
    if ha["backend"] == "openai":
        with run.step("model-setup", "local model server") as s:
            s.touch("install a local model server and download a model")
            info = models.install_llama_server(host, model["local_model"], log=ctx.log)
            run.env["local_model"] = info["model"]
            run.env["local_model_speed"] = f"{info['tokens_per_s']} tokens/s generation, {info['prompt_tokens_per_s']} tokens/s prompt"
            run.env["local_model_rss_mb"] = info["server_rss_mb"]
            run.env["free_mb_before_model"] = info["free_mb_before"]
            s.note(f"{info['model']}: {info['tokens_per_s']} tok/s, {info['server_rss_mb']} MB")
            ctx.local_base = info["base_url"]
    with run.step("model-setup", "turn on the house agent") as s:
        s.touch("Manage > Home agent: enable it and pick the backend")
        sets = [("home.enabled", True), ("home.tools", True)]
        if ha["backend"] == "claude-code":
            s.touch("pick the agent host and model")
            sets += [("home.backend", "claude-code"), ("home.agent_host", host.name),
                     ("home.claude_model", ha.get("model", "haiku"))]
        else:
            if ctx.hub_machine.name != host.name:
                s.note("local model lives on a worker, not the hub: base_url must be that machine's address")
            s.touch("type the model server's address and model name")
            sets += [("home.backend", "openai"), ("home.base_url", getattr(ctx, "local_base", "")),
                     ("home.model", "local"), ("home.timeout_s", 300)]
        for k, v in sets:
            r = hub.hub_cap("settings.set", {"key": k, "value": v, "note": "cleanroom"})
            if not r.get("ok"):
                s.fail(f"{k}: {json.dumps(r)[:200]}")
        st = (hub.hub_cap("home.status", {}).get("result") or {})
        if st.get("missing"):
            s.note(f"house agent says missing: {st['missing']}")
        test = hub.hub_cap("home.ask", {"question": "Say hello in five words.", "source": "cleanroom"}, timeout=400)
        res = test.get("result") or {}
        if not test.get("ok") or not res.get("answer"):
            err = json.dumps(test)[:400]
            if "public URL" in err or "callback" in err:
                run.findings.append("The house agent's agent-host turns fail on a fresh hub until "
                                    "home.agent_callback_url is set by hand.")
                s.touch("find and set the house agent's callback URL")
                hub.hub_cap("settings.set", {"key": "home.agent_callback_url",
                                             "value": f"http://{ctx.hub_machine.host}:7005", "note": "cleanroom"})
                test = hub.hub_cap("home.ask", {"question": "Say hello in five words.", "source": "cleanroom"}, timeout=400)
                res = test.get("result") or {}
            if not res.get("answer"):
                s.fail(json.dumps(test)[:400])
        s.note(f"test answer in {res.get('latency_ms')} ms ({res.get('model')}): {str(res.get('answer'))[:80]}")


# ---------------------------------------------------------------------------
# 5. First text request to the house agent
# ---------------------------------------------------------------------------

def _ask(ctx, text, *, timeout=600):
    t0 = time.time()
    r = ctx.hub.hub_cap("home.ask", {"question": text, "source": "cleanroom"}, timeout=timeout)
    res = r.get("result") or {}
    res["_wall_s"] = round(time.time() - t0, 1)
    if not r.get("ok"):
        res["_error"] = json.dumps(r.get("error") or r)[:400]
    return res


def _say_yes(ctx, step, res) -> dict:
    """The person answers Yes to the house agent's ask on the Decisions page."""
    step.touch("answer Yes to the house agent's one-line ask")
    if not ctx.hub._cookie:
        ctx.hub.login()
    out = ctx.hub.account_post("/account/huddle/api", {"action": "front_answer",
                                                        "ask": res["asks"][0], "decision": "yes"},
                               timeout=300)
    step.note("answered Yes: " + json.dumps(out)[:200])
    return out if isinstance(out, dict) else {}


def first_request(ctx: Ctx):
    run = ctx.run
    names = [m.name for m in ctx.worker_machines()] + ([PHONE_NAME] if ctx.emu else [])
    with run.step("first-request", "ask which machines are on the band") as s:
        res = _ask(ctx, "Which machines are on my band right now? One short sentence.")
        ans = str(res.get("answer", ""))
        s.note(f"{res['_wall_s']} s, path {res.get('path', '-')}, tools {res.get('tools_used')}: {ans[:160]!r}")
        if res.get("_error"):
            s.fail(res["_error"])
        hit = [n for n in names if n.lower() in ans.lower()]
        if not hit:
            run.findings.append("The house agent cannot answer 'which machines are on my band?' on a fresh "
                                "hub: it has no band-status tool (front door or not).")
            s.fail("the answer names none of the band's machines")
        run.mark_first_useful()
    with run.step("first-request", "front door: a quick action") as s:
        on = ctx.hub.hub_cap("settings.set", {"key": "home.front_door", "value": True, "note": "cleanroom"})
        if not on.get("ok"):
            s.skip("no front door on this hub")
        res = _ask(ctx, "Which machines are on my band right now? One short sentence.")
        ans = str(res.get("answer", ""))
        hit = [n for n in names if n.lower() in ans.lower()]
        s.note(f"machines again, front door on: {res['_wall_s']} s, tools {res.get('tools_used')}, "
               f"named {hit}: {ans[:120]!r}")
        if hit:
            run.mark_first_useful()
        m = ctx.agent_host
        approver = PersonApprover(ctx.hub, s, caps=("shell.", "work.", "sys."))
        approver.start()
        try:
            res = _ask(ctx, f"How long has {m.name} been up? Run uptime on it and tell me.")
        finally:
            approver.stop()
        ans = str(res.get("answer", ""))
        s.note(f"{res['_wall_s']} s, path {res.get('path')}, done {res.get('done')}, asks {res.get('asks')}: {ans[:200]!r}")
        if res.get("_error"):
            s.fail(res["_error"])
        if res.get("path") == "ask" and res.get("asks"):
            out = _say_yes(ctx, s, res)
            if not json.dumps(out).count("up"):
                s.fail("the approved action gave no uptime")
            run.mark_first_useful()
        elif res.get("path") != "do" or "up" not in ans.lower():
            s.fail("expected the do path with an uptime answer")
        else:
            run.mark_first_useful()


# ---------------------------------------------------------------------------
# 6. Fix a bug: session -> PR
# ---------------------------------------------------------------------------

def _gh(*args, check=True, input=None, cwd=None):
    p = subprocess.run(["gh", *args], capture_output=True, text=True, input=input, cwd=cwd, timeout=300)
    if check and p.returncode:
        raise RuntimeError(f"gh {args[:3]}: {p.stderr[-300:]}")
    return p.stdout


def reset_toy_repo(repo: str):
    """Force the toy repo back to its seed: master = the buggy seed, no PRs, no branches."""
    for n in json.loads(_gh("pr", "list", "-R", repo, "--state", "open", "--json", "number") or "[]"):
        _gh("pr", "close", str(n["number"]), "-R", repo, "--delete-branch", check=False)
    with tempfile.TemporaryDirectory() as d:
        for path, text in toy_seed.FILES.items():
            os.makedirs(os.path.join(d, os.path.dirname(path)), exist_ok=True)
            open(os.path.join(d, path), "w").write(text)
        g = lambda *a: subprocess.run(["git", "-C", d, *a], check=True, capture_output=True, text=True)
        g("init", "-q", "-b", "master")
        g("add", "-A")
        g("-c", "user.name=rook-cleanroom", "-c", "user.email=cleanroom@localhost",
          "commit", "-q", "-m", "Seed: toy stats module (median has a bug)")
        g("push", "-q", "--force", f"https://github.com/{repo}.git", "master")
    for b in _gh("api", f"repos/{repo}/branches", "--jq", ".[].name", check=False).split():
        if b != "master":
            _gh("api", "-X", "DELETE", f"repos/{repo}/git/refs/heads/{b}", check=False)


class PersonApprover:
    """A person watching the Approvals page who approves holds on the given
    caps as they appear (each one a human touch on the step)."""

    def __init__(self, hub: Hub, step, caps=()):
        self.hub, self.step, self.caps = hub, step, tuple(caps)
        self.approved: list[str] = []
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self._t.start()

    def stop(self):
        self._stop.set()
        self._t.join(10)

    def _loop(self):
        seen = set()
        while not self._stop.wait(4):
            try:
                for a in self.hub.approvals():
                    if a["id"] in seen or not str(a.get("cap", "")).startswith(self.caps):
                        continue
                    seen.add(a["id"])
                    self.hub.decide(a["id"], "approve")
                    self.approved.append(f"{a.get('cap')} on {a.get('target_name')} ({a.get('reason')})")
                    self.step.touch(f"approve {a.get('cap')} on {a.get('target_name')}: {a.get('reason')}")
            except Exception:  # noqa: BLE001 - keep watching
                pass


def _sessions(hub: Hub) -> list[dict]:
    rows = hub.hub_cap("role.sessions", {}).get("result") or {}
    return list(rows.get("sessions", rows) if isinstance(rows, dict) else rows or [])


def _newest_session(hub: Hub, since: float, wait: float = 20) -> dict | None:
    deadline = time.time() + wait
    while True:
        rows = [r for r in _sessions(hub) if float(r.get("started") or 0) >= since - 2]
        if rows:
            return max(rows, key=lambda r: float(r.get("started") or 0))
        if time.time() > deadline:
            return None
        time.sleep(3)


def _wait_pr(hub: Hub, session: dict | None, *, timeout: float) -> tuple[str, dict | None]:
    if not session:
        return "", None
    deadline = time.time() + timeout
    while time.time() < deadline:
        cur = next((r for r in _sessions(hub) if r.get("id") == session.get("id")), session)
        session = cur
        pr = cur.get("pr_url") or cur.get("pr") or ""
        if not pr:
            m = re.search(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+", str(cur.get("status") or ""))
            pr = m.group(0) if m else ""
        if pr:
            return pr, cur
        if cur.get("state") in ("failed", "stopped", "ended", "done", "blocked"):
            return "", cur
        time.sleep(15)
    return "", session


def fix_bug(ctx: Ctx):
    run, hub, m = ctx.run, ctx.hub, ctx.agent_host
    repo = ctx.site.get("toy_repo")
    if (ctx.model.get("sessions") or {}).get("harness") != "claude":
        with run.step("fix-bug", "session to a PR") as s:
            s.skip("this model profile has no session harness (coding sessions run Claude Code only)")
        return
    if not repo:
        with run.step("fix-bug", "session to a PR") as s:
            s.skip("no toy_repo in the site config")
        return
    with run.step("fix-bug", "toy repo on the laptop") as s:
        reset_toy_repo(repo)
        token = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()
        s.touch("install GitHub CLI and sign in (gh auth login)")
        m.sh("command -v gh >/dev/null || (sudo DEBIAN_FRONTEND=noninteractive apt-get -qq install -y gh git >/dev/null)",
             timeout=600)
        m.sh("gh auth login --with-token >/dev/null 2>&1 && gh auth setup-git", input=token + "\n")
        s.touch("clone the repo")
        m.sh(f"rm -rf ~/toy && git clone -q https://github.com/{repo}.git ~/toy && "
             "git -C ~/toy config user.name 'Rook session' && git -C ~/toy config user.email session@localhost")
        s.touch("Settings > Roles: say where the code lives")
        line = f"toy worker {m.name} /home/{m.user}/toy worktrees go under /home/{m.user}/toy-wt origin/master"
        r = hub.hub_cap("settings.set", {"key": "role.code_locations", "value": [line], "note": "cleanroom"})
        if not r.get("ok"):
            s.fail(json.dumps(r)[:300])
        t = hub.hub_cap("settings.set", {"key": "role.session_test_commands",
                                         "value": [toy_seed.TEST_COMMAND], "note": "cleanroom"})
        if t.get("ok"):
            s.touch("Settings > Roles: allow the repo's test command")
        else:
            s.note("this hub has no role.session_test_commands: sessions can only run rook-pytest")
        s.touch("Manage > Roles: add a lead role from the presets")
        pre = (hub.hub_cap("role.presets", {"id": "lead", "full": True}).get("result") or {})
        role = dict(pre.get("preset") or pre.get("role") or {})
        if not role:
            s.fail("no lead preset: " + json.dumps(pre)[:200])
        role["host"] = m.name
        rs = hub.hub_cap("role.set", {"role": role, "note": "cleanroom"})
        if not rs.get("ok"):
            s.fail("role.set: " + json.dumps(rs)[:300])
    if run.steps[-1].status != "pass":
        return
    with run.step("fix-bug", "ask the house agent to fix it") as s:
        t0 = time.time()
        approver = PersonApprover(hub, s, caps=("work.stream.", "role.session", "git."))
        approver.start()
        try:
            res = _ask(ctx, f"In the toy repo: {toy_seed.BUG} Please get it fixed and open a pull request.")
            s.note(f"house agent: {res['_wall_s']} s, path {res.get('path')}, refs {res.get('refs')}: "
                   f"{str(res.get('answer'))[:160]!r}")
            if res.get("path") == "ask" and res.get("asks"):
                _say_yes(ctx, s, res)
            session = _newest_session(hub, t0)
            if session is None or session.get("state") == "failed":
                if session is not None:
                    s.note(f"dispatched session {session.get('name')} {session.get('state')}: "
                           f"{str(session.get('status'))[:120]}")
                s.note("starting the session directly (role.session_start)")
                s.touch("start the coding session by hand")
                t1 = time.time()
                st = hub.hub_cap("role.session_start", {"role": "lead", "branch": f"fix/median-{int(t1) % 100000}",
                                                        "instructions": toy_seed.BUG + " Open a pull request.",
                                                        "location": "toy"}, timeout=180)
                if not st.get("ok"):
                    s.fail("role.session_start: " + json.dumps(st)[:400])
                session = _newest_session(hub, t1)
            pr, session = _wait_pr(hub, session, timeout=1500)
            if not pr and session and session.get("state") in ("blocked", "stopped", "done"):
                s.note(f"session {session.get('name')} {session.get('state')}: "
                       f"{str(session.get('status'))[:160]}")
                s.touch("answer the session's question")
                sent = hub.hub_cap("role.session_send", {"id": session["id"], "text": (
                    "Go ahead: if you cannot run the tests, say so in the pull request and open "
                    "it anyway.")}, timeout=180)
                if sent.get("ok"):
                    time.sleep(15)
                    pr, session = _wait_pr(hub, dict(session, state="running"), timeout=1200)
                else:
                    s.note("role.session_send: " + json.dumps(sent)[:200])
        finally:
            approver.stop()
        if session:
            s.note(f"session {session.get('name')}: state {session.get('state')}, "
                   f"status {str(session.get('status'))[:140]!r}")
            for k in ("cost", "cost_usd"):
                if isinstance(session.get(k), (int, float)):
                    s.tokens(cost_usd=session[k])
        if approver.approved:
            s.note("holds approved by the person: " + ", ".join(approver.approved))
        if not pr:
            s.fail("no pull request within 25 minutes")
        s.note(f"PR {pr} {time.time() - t0:.0f} s after asking")
        run.mark_first_useful()
    if run.steps[-1].status != "pass":
        return
    with run.step("fix-bug", "the PR fixes the bug") as s:
        num = re.search(r"/pull/(\d+)", pr).group(1)
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(["git", "clone", "-q", f"https://github.com/{repo}.git", d], check=True)
            subprocess.run(["gh", "pr", "checkout", num, "-R", repo], cwd=d, check=True, capture_output=True)
            t = subprocess.run(["python3", "-m", "unittest", "-q"], cwd=d, capture_output=True, text=True)
            if t.returncode:
                s.fail("tests still fail on the PR branch: " + t.stderr[-300:])
            diff = subprocess.run(["git", "diff", "--stat", "origin/master"], cwd=d, capture_output=True, text=True).stdout
            s.note("tests pass on the PR; " + diff.strip().splitlines()[-1] if diff.strip() else "")


# ---------------------------------------------------------------------------
# 7. Approval path
# ---------------------------------------------------------------------------

def apply_trust(ctx: Ctx):
    """The trust preset, set the way a person would on the dashboard."""
    t, hub = ctx.trust, ctx.hub
    with ctx.run.step("trust", f"apply preset {ctx.run.trust}") as s:
        if ctx.run.trust != "balanced":
            s.touch("Settings/Trust: choose the preset")
        r = hub.hub_cap("settings.set", {"key": "core.approvals.enabled", "value": bool(t["approvals_enabled"]),
                                         "note": "cleanroom"})
        if not r.get("ok"):
            s.note("core.approvals.enabled: " + json.dumps(r)[:200])
        try:
            hub.trust("set_unlisted", {"level": t["default_level"]}, f"clean-room preset {ctx.run.trust}")
        except HubError as e:
            s.note(f"trust map: {e}"[:300])
        tr = hub.hub_cap("trust.get", {}).get("result") or {}
        s.note(f"unlisted={tr.get('unlisted', (tr.get('map') or {}).get('unlisted'))}, "
               f"approvals.enabled={t['approvals_enabled']}")


def approval(ctx: Ctx):
    run, hub = ctx.run, ctx.hub
    m = ctx.agent_host
    with run.step("approval", "an agent's admin call") as s:
        args = {"key": "hub.motd", "value": f"cleanroom {int(time.time())}", "note": "cleanroom approval test"}
        r = hub.mcp_call("settings.set", args)
        held = isinstance(r, dict) and (r.get("held") or (r.get("result") or {}).get("held"))
        hold_id = isinstance(r, dict) and (r.get("hold") or r.get("hold_id") or r.get("id"))
        s.note(f"first try: {'held' if held else 'ran'}: {json.dumps(r)[:200]}")
        expect_held = ctx.run.trust != "yolo"
        if not held:
            if expect_held and ctx.trust["approvals_enabled"]:
                s.fail("expected a hold for an agent's admin call")
            return
        s.touch("approve the hold on the Approvals page")
        items = hub.approvals()
        item = next((i for i in items if i.get("id") == hold_id), None) or next(
            (i for i in items if i.get("cap") == "settings.set"), None)
        if not item:
            s.fail("the hold is not on the Approvals page")
        hub.decide(item.get("id") or hold_id, "approve")
        r2 = hub.mcp_call("settings.set", args)
        ok = isinstance(r2, dict) and r2.get("ok") and not r2.get("held")
        if not ok:
            s.fail("the approved call did not run: " + json.dumps(r2)[:300])
        s.note("approved call ran once")
    with run.step("approval", "votes (H6)") as s:
        s.skip("votes are not built on master yet (H6, phase 5)")


# ---------------------------------------------------------------------------
# 8. Failure partway: kill the worker mid-call
# ---------------------------------------------------------------------------

def worker_failure(ctx: Ctx):
    run, hub = ctx.run, ctx.hub
    m = ctx.agent_host
    with run.step("worker-failure", f"kill {m.name}'s worker mid-call") as s:
        out: dict = {}

        def slow():
            try:
                out["r"] = hub.band_call("shell.exec", {"argv": ["sleep", "40"], "timeout": 50}, worker=m.name, timeout=60)
            except Exception as e:  # noqa: BLE001
                out["e"] = str(e)
            out["done"] = time.time()
        th = threading.Thread(target=slow)
        t0 = time.time()
        th.start()
        time.sleep(5)
        m.sh("pkill -KILL -f 'band-worker|rook.worker|rook-worker' || true", check=False)
        t1 = time.time()
        back = None
        while time.time() - t1 < 180:
            try:
                r = hub.band_call("shell.exec", {"argv": ["true"], "timeout": 10}, worker=m.name, timeout=20)
                if r.get("ok"):
                    back = time.time() - t1
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(2)
        th.join(130)
        waited = (out.get("done") or time.time()) - t1
        s.note(f"in-flight call returned {waited:.0f} s after the kill: "
               + (out.get("e") or json.dumps(out.get("r"))[:160]))
        if waited > 30:
            run.findings.append(f"A call in flight on a worker that dies is not failed when the worker drops: "
                                f"the caller waits out the call's own timeout ({waited:.0f} s here).")
        if back is None:
            s.fail("the worker did not come back within 3 minutes")
        s.note(f"worker answered again {back:.0f} s after the kill")
    others = [w for w in ctx.worker_machines() if w.name != ctx.hub_machine.name and w is not m]
    if others:
        w = others[-1]
        with run.step("worker-failure", f"power {w.name} off hard, then on") as s:
            ctx.vms.stop(w)
            hub.wait_worker(w.name, timeout=200, online=False)
            t1 = time.time()
            ctx.vms.start(w)
            hub.wait_worker(w.name, timeout=300)
            s.note(f"{w.name} back on the band {time.time() - t1:.0f} s after power-on (no person needed)")
    if ctx.emu and hub.worker(PHONE_NAME):
        with run.step("worker-failure", "phone loses its network for 60 s") as s:
            emu = ctx.emu
            emu.adb("shell", "svc", "wifi", "disable", check=False)
            emu.adb("shell", "svc", "data", "disable", check=False)
            time.sleep(60)
            emu.adb("shell", "svc", "wifi", "enable", check=False)
            emu.adb("shell", "svc", "data", "enable", check=False)
            t1 = time.time()
            ok = False
            while time.time() - t1 < 240:
                try:
                    r = hub.band_call("caps.describe", {"prefix": "device.battery"}, worker=PHONE_NAME, timeout=30)
                    if r.get("ok"):
                        ok = True
                        break
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(5)
            if not ok:
                s.fail("the phone did not answer within 4 minutes of the network coming back")
            s.note(f"phone answered {time.time() - t1:.0f} s after the network came back")


# ---------------------------------------------------------------------------
# 9. Learn a preference
# ---------------------------------------------------------------------------

def learn_preference(ctx: Ctx):
    run, hub = ctx.run, ctx.hub
    m = ctx.agent_host
    with run.step("learn-preference", "say a preference") as s:
        st = hub.hub_cap("home.playbook", {})
        if not st.get("ok"):
            s.skip("no playbook on this hub (front door not landed)")
        res = _ask(ctx, f"From now on, ask me before you run anything on {m.name}.")
        s.note(f"{res['_wall_s']} s, path {res.get('path')}, tools {res.get('tools_used')}: {str(res.get('answer'))[:160]!r}")
        if res.get("path") == "ask" and res.get("asks"):
            s.touch("answer Yes to the house agent's question about the new rule")
            ans = hub.account_post("/account/huddle/api", {"action": "front_answer",
                                                            "ask": res["asks"][0], "decision": "yes"})
            s.note("answered Yes: " + json.dumps(ans)[:160])
        pb = json.dumps(hub.hub_cap("home.playbook", {}).get("result"))
        if m.name.lower() not in pb.lower():
            s.fail("the playbook has no rule naming the machine")
    with run.step("learn-preference", "the preference holds next time") as s:
        res = _ask(ctx, f"Run uptime on {m.name}.")
        s.note(f"{res['_wall_s']} s, path {res.get('path')}, asks {res.get('asks')}: {str(res.get('answer'))[:160]!r}")
        if res.get("path") != "ask":
            s.fail("expected an ask (the preference says ask first)")


# ---------------------------------------------------------------------------
# 10. Voice
# ---------------------------------------------------------------------------

def voice(ctx: Ctx):
    with ctx.run.step("voice", "first voice request") as s:
        st = ctx.hub.get_json("/api/band/workers")
        s.skip("a fresh hub has no voice server (speech-to-text and text-to-speech run on a separate "
               "voice host); synthetic audio has nothing to talk to in this profile")


ORDER = [
    ("onboard-hub", onboard_hub),
    ("pair-laptop", pair_workers),
    ("pair-phone", pair_phone),
    ("trust", apply_trust),
    ("model-setup", model_setup),
    ("first-request", first_request),
    ("approval", approval),
    ("fix-bug", fix_bug),
    ("learn-preference", learn_preference),
    ("worker-failure", worker_failure),
    ("voice", voice),
]


def usage_tokens(ctx: Ctx) -> dict:
    """The hub's own 24 h agent-host usage ledger (house agent + roles)."""
    try:
        v = ctx.hub.settings_view("home")
    except Exception:  # noqa: BLE001
        return {}
    rows = v.get("usage") or (v.get("home") or {}).get("usage") or []
    tot: dict = {}
    for r in rows:
        for k, n in (r.get("tokens") or {}).items():
            if isinstance(n, (int, float)):
                tot[k] = tot.get(k, 0) + n
        if isinstance(r.get("cost_usd"), (int, float)):
            tot["cost_usd"] = round(tot.get("cost_usd", 0) + r["cost_usd"], 4)
        tot["turns"] = tot.get("turns", 0) + int(r.get("turns") or 0)
    return tot
