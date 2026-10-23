"""What a hub release serves besides its code: the worker bundle and the APKs.

A dashboard serves ``/band-worker.pyz`` + ``/band-worker.json`` from an
operator-built bundle in ``rook/remote/`` (else it builds its own), and
``/apk`` + ``/apk.json`` from ``rook/remote/rook-worker.apk`` (approved by
``ROOK_PUBLIC_APK_SHA256``). Those files are not in git, so a release that is
only ``git archive`` drops them, and every deploy used to copy them in by hand.

A release now names them in its signed manifest (``feed``: one entry per file,
path + sha256 + size) and ships each file beside the tarball, stored once by
content (``feed/<sha256>``) so a 145 MB APK that several releases carry is on
disk once. ``rook hub deploy`` fetches and checks each file and links it into
the release tree (``rook/remote/...``; the beta APK under
``rook/remote/apk-beta/``). Rolling back to a release brings back its feed too.

Kinds (each optional):

* ``worker``: band-worker.pyz + band-worker.json (+ band-worker-enrollment.pyz).
  The manifest must verify against the trusted update key, and the bundle must
  match its sha256: the same checks a worker makes before it installs one.
* ``apk``: rook-worker.apk + rook-worker-apk.json, the general (stable) app.
* ``apk_beta``: the beta channel's app, same files.

The SHA gates are never changed here: a deploy refuses a release whose APK
``ROOK_PUBLIC_APK_SHA256`` (or whose beta ``ROOK_BETA_APK_SHA256``) would not
approve, instead of letting /apk go dark (see :func:`gate_problems`).
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from rook.worker._update_verify import sha256_file

REMOTE = "rook/remote"
BETA = "rook/remote/apk-beta"
PYZ, PYZ_MANIFEST, ENROLL = "band-worker.pyz", "band-worker.json", "band-worker-enrollment.pyz"
APK, APK_MANIFEST = "rook-worker.apk", "rook-worker-apk.json"
PACKAGE = "systems.bake.rook"
KINDS = ("worker", "apk", "apk_beta")

# Every path a feed entry may have: a signed manifest still cannot write
# anywhere else in a release tree.
PATHS = {
    f"{REMOTE}/{PYZ}": "worker", f"{REMOTE}/{PYZ_MANIFEST}": "worker",
    f"{REMOTE}/{ENROLL}": "worker",
    f"{REMOTE}/{APK}": "apk", f"{REMOTE}/{APK_MANIFEST}": "apk",
    f"{BETA}/{APK}": "apk_beta", f"{BETA}/{APK_MANIFEST}": "apk_beta",
}
KIND_DIR = {"worker": REMOTE, "apk": REMOTE, "apk_beta": BETA}
BLOB_DIR = "feed"


class FeedError(Exception):
    pass


def _read_json(p: Path):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# -- what a directory holds ----------------------------------------------------------

def worker_files(src: Path, pubkey: str | None) -> tuple[list[tuple[str, Path]], dict]:
    """The bundle in ``src`` as [(feed path, file)] plus its summary. Refuses an
    unsigned bundle, one not signed by ``pubkey``, or one whose pyz does not
    match its manifest."""
    src = Path(src)
    pyz, man = src / PYZ, src / PYZ_MANIFEST
    m = _read_json(man)
    if not pyz.is_file() or not isinstance(m, dict):
        raise FeedError(f"no worker bundle in {src} ({PYZ} + {PYZ_MANIFEST})")
    if not pubkey:
        raise FeedError("no trusted update key to check the worker bundle against")
    from rook.worker._update_verify import verify_manifest
    if not m.get("sig") or not verify_manifest(m, pubkey):
        raise FeedError(f"{man} is not signed by the trusted update key")
    if m.get("sha256") != sha256_file(pyz) or m.get("size") != pyz.stat().st_size:
        raise FeedError(f"{pyz} does not match its signed manifest")
    files = [(f"{REMOTE}/{PYZ}", pyz), (f"{REMOTE}/{PYZ_MANIFEST}", man)]
    if (src / ENROLL).is_file():
        files.append((f"{REMOTE}/{ENROLL}", src / ENROLL))
    return files, {"build": m.get("build"), "version": m.get("version"),
                   "sha256": m["sha256"], "url": m.get("url", "")}


def check_apk_manifest(apk: Path, m) -> dict:
    """``m`` if it describes ``apk`` the way the dashboard requires, else FeedError."""
    if (not isinstance(m, dict) or m.get("package") != PACKAGE
            or type(m.get("version_code")) is not int or m["version_code"] <= 0
            or not isinstance(m.get("version_name"), str)):
        raise FeedError(f"{apk.parent / APK_MANIFEST} is not a {PACKAGE} APK manifest")
    if m.get("size") != apk.stat().st_size or m.get("sha256") != sha256_file(apk):
        raise FeedError(f"{apk} does not match its manifest (size/sha256)")
    return m


def apk_files(src: Path, kind: str = "apk") -> tuple[list[tuple[str, Path]], dict]:
    src = Path(src)
    apk = src / APK
    if not apk.is_file():
        raise FeedError(f"no {APK} in {src}")
    m = check_apk_manifest(apk, _read_json(src / APK_MANIFEST))
    d = KIND_DIR[kind]
    return ([(f"{d}/{APK}", apk), (f"{d}/{APK_MANIFEST}", src / APK_MANIFEST)],
            {"version_name": m["version_name"], "version_code": m["version_code"],
             "sha256": m["sha256"]})


def build_worker(repo: Path, commit: str, out: Path, signing_key,
                 public_base: str = "") -> Path:
    """Build the worker bundle of ``commit`` (a throwaway git worktree, so the
    build number and stamped commit are that commit's, never a dirty tree's)
    into ``out``, signed with ``signing_key`` (the release's key; the builder
    gets it as a private temporary key file, so it neither creates a key nor
    uses another one). Returns ``out``."""
    import base64
    repo, out = Path(repo).resolve(), Path(out).resolve()
    with tempfile.TemporaryDirectory(prefix="rook-feed-") as tmp:
        keyfile = Path(tmp) / "update-signing-key"
        fdk = os.open(keyfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fdk, "w") as f:
            f.write(base64.b64encode(bytes(signing_key)).decode("ascii") + "\n")
        wt = Path(tmp) / "src"
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(wt),
                        commit], check=True, capture_output=True, text=True)
        try:
            env = {k: v for k, v in os.environ.items() if k != "ROOK_PUBLIC_BASE"}
            if public_base:
                env["ROOK_PUBLIC_BASE"] = public_base.rstrip("/")
            env["PYTHONPATH"] = str(wt)
            env["ROOK_UPDATE_KEY"] = str(keyfile)
            env["ROOK_UPDATE_PUBKEY"] = base64.b64encode(
                bytes(signing_key.verify_key)).decode("ascii")
            r = subprocess.run([sys.executable, str(wt / "rook/remote/build_band_worker.py"),
                                "--output-dir", str(out)], env=env, cwd=str(wt),
                               capture_output=True, text=True, timeout=600)
            if r.returncode != 0:
                raise FeedError("worker bundle build failed: "
                                + (r.stderr or r.stdout).strip()[-600:])
        finally:
            subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)],
                           capture_output=True)
    m = _read_json(out / PYZ_MANIFEST) or {}
    if not m.get("sig"):
        raise FeedError("the worker bundle came out unsigned (no update signing key?)")
    return out


# -- blobs -----------------------------------------------------------------------------

def _link_or_copy(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.tmp{os.getpid()}")
    tmp.unlink(missing_ok=True)
    try:
        os.link(src, tmp)
    except OSError:
        shutil.copyfile(src, tmp)
    os.replace(tmp, dest)


def store(files: list[tuple[str, Path]], kind: str, out_dir: Path) -> list[dict]:
    """Put each file in ``out_dir/feed/<sha256>`` and return its feed entries."""
    entries = []
    for path, f in files:
        if PATHS.get(path) != kind:
            raise FeedError(f"{path} is not a {kind} feed path")
        sha = sha256_file(f)
        blob = Path(out_dir) / BLOB_DIR / sha
        if not (blob.is_file() and sha256_file(blob) == sha):
            _link_or_copy(f, blob)
        entries.append({"kind": kind, "path": path, "sha256": sha, "size": f.stat().st_size})
    return entries


def check_entries(entries) -> None:
    """Shape checks for a manifest's ``feed`` (its signature covers the rest)."""
    if not isinstance(entries, list):
        raise FeedError("feed must be a list")
    seen = set()
    for e in entries:
        if not isinstance(e, dict) or PATHS.get(e.get("path")) != e.get("kind"):
            raise FeedError(f"bad feed entry {e!r}")
        if not isinstance(e.get("sha256"), str) or len(e["sha256"]) != 64 \
                or any(c not in "0123456789abcdef" for c in e["sha256"]):
            raise FeedError(f"feed entry {e['path']}: bad sha256")
        if type(e.get("size")) is not int or e["size"] < 0:
            raise FeedError(f"feed entry {e['path']}: bad size")
        if e["path"] in seen:
            raise FeedError(f"feed entry {e['path']} appears twice")
        seen.add(e["path"])


def blob_source(manifest: dict, manifest_src: str, sha: str) -> str:
    """Where a feed blob comes from: beside the signed tarball URL, else beside
    the manifest (same directory or same URL base)."""
    if manifest.get("url"):
        return urllib.parse.urljoin(str(manifest["url"]), f"{BLOB_DIR}/{sha}")
    if urllib.parse.urlparse(manifest_src).scheme in ("http", "https"):
        return urllib.parse.urljoin(manifest_src, f"{BLOB_DIR}/{sha}")
    return str(Path(manifest_src).expanduser().resolve().parent / BLOB_DIR / sha)


def fetch(manifest: dict, manifest_src: str, dest_dir: Path) -> dict[str, Path]:
    """Fetch every feed blob into ``dest_dir`` (by sha256) and check it.
    Returns {feed path: blob}."""
    out = {}
    entries = manifest.get("feed") or []
    check_entries(entries)
    for e in entries:
        blob = Path(dest_dir) / e["sha256"]
        if not (blob.is_file() and blob.stat().st_size == e["size"]
                and sha256_file(blob) == e["sha256"]):
            src = blob_source(manifest, manifest_src, e["sha256"])
            blob.parent.mkdir(parents=True, exist_ok=True)
            tmp = blob.with_name(f".{blob.name}.part{os.getpid()}")
            try:
                if urllib.parse.urlparse(src).scheme in ("http", "https"):
                    with urllib.request.urlopen(src, timeout=600) as r, open(tmp, "wb") as f:  # noqa: S310
                        shutil.copyfileobj(r, f, 1 << 20)
                else:
                    try:
                        os.link(src, tmp)
                    except OSError:
                        shutil.copyfile(src, tmp)
            except OSError as ex:
                tmp.unlink(missing_ok=True)
                raise FeedError(f"cannot fetch {e['path']} from {src}: {ex}") from None
            if tmp.stat().st_size != e["size"] or sha256_file(tmp) != e["sha256"]:
                tmp.unlink(missing_ok=True)
                raise FeedError(f"{e['path']} from {src} does not match the signed manifest")
            os.chmod(tmp, 0o644)
            os.replace(tmp, blob)
        out[e["path"]] = blob
    return out


def install(blobs: dict[str, Path], tree: Path) -> None:
    """Link (else copy) each blob to its path in an unpacked release tree."""
    for path, blob in blobs.items():
        _link_or_copy(blob, Path(tree) / path)


# -- what a release tree serves ----------------------------------------------------------

def tree_kinds(tree: Path) -> set[str]:
    tree = Path(tree)
    have = set()
    if (tree / REMOTE / PYZ).is_file() and (tree / REMOTE / PYZ_MANIFEST).is_file():
        have.add("worker")
    if (tree / REMOTE / APK).is_file() and (tree / REMOTE / APK_MANIFEST).is_file():
        have.add("apk")
    if (tree / BETA / APK).is_file() and (tree / BETA / APK_MANIFEST).is_file():
        have.add("apk_beta")
    return have


def tree_files(tree: Path, kind: str) -> list[tuple[str, Path]]:
    tree = Path(tree)
    return [(p, tree / p) for p, k in PATHS.items() if k == kind and (tree / p).is_file()]


def carry(src_tree: Path, dest_tree: Path, kinds, log) -> list[str]:
    """Copy (link) the feed files of ``kinds`` from the running release into a
    new one that does not carry them, so a deploy never drops what is served."""
    carried = []
    for kind in kinds:
        files = tree_files(src_tree, kind)
        if not files:
            continue
        for path, f in files:
            _link_or_copy(f, Path(dest_tree) / path)
        carried.append(kind)
        log(f"feed: carried {kind} from {Path(src_tree).name} "
            f"({', '.join(Path(p).name for p, _ in files)})")
    if carried:
        note = Path(dest_tree) / "FEED-CARRIED.json"
        old = _read_json(note) or {}
        old.update({k: Path(src_tree).name for k in carried})
        note.write_text(json.dumps(old, indent=2) + "\n", encoding="utf-8")
    return carried


def apk_summary(tree: Path, kind: str) -> dict | None:
    d = Path(tree) / KIND_DIR[kind]
    m = _read_json(d / APK_MANIFEST)
    if not (d / APK).is_file() or not isinstance(m, dict):
        return None
    return m


# -- the gates (environment of the service that serves the feed) ---------------------------

def service_env(systemctl: list[str] | None) -> dict:
    """``Environment=`` of a systemd unit (``systemctl show`` argv), else this
    process's environment (a hub not run by systemd shares the deployer's)."""
    if not systemctl:
        return dict(os.environ)
    try:
        r = subprocess.run(systemctl, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    env = {}
    for tok in shlex.split(r.stdout or ""):
        k, sep, v = tok.partition("=")
        if sep:
            env[k] = v
    return env


def updates_dir(env: dict) -> Path | None:
    if env.get("ROOK_UPDATES_DIR"):
        return Path(env["ROOK_UPDATES_DIR"])
    if env.get("ROOK_DATA_DIR"):
        return Path(env["ROOK_DATA_DIR"]) / "updates"
    return None


def _sha_of(d: Path | None) -> str:
    if d is None:
        return ""
    m = _read_json(d / APK_MANIFEST)
    return str(m.get("sha256") or "") if isinstance(m, dict) and (d / APK).is_file() else ""


def expected(tree: Path, env: dict) -> dict:
    """What the dashboard on ``tree`` with ``env`` will serve, and what is wrong.

    {"problems": [...], "warnings": [...], "apk": sha or "", "apk_public": bool,
     "apk_beta": sha or "", "worker": summary or None}"""
    tree = Path(tree)
    probs, warns = [], []
    upd = updates_dir(env)
    gate = (env.get("ROOK_PUBLIC_APK_SHA256") or "").strip()
    beta_gate = (env.get("ROOK_BETA_APK_SHA256") or "").strip()
    rel_apk = _sha_of(tree / REMOTE)
    published = _sha_of(upd / "apk") if upd else ""
    apk, public = "", False
    if gate:
        if published and published == gate:
            apk, public = published, True
        elif rel_apk == gate:
            apk, public = rel_apk, True
        else:
            probs.append(f"ROOK_PUBLIC_APK_SHA256 ({gate[:12]}) approves neither this release's APK "
                         f"({rel_apk[:12] or 'none'}) nor a published one "
                         f"({published[:12] or 'none'}): /apk would stop answering. Build the "
                         "release with the approved APK (--apk), or change the gate yourself first")
    elif published:
        apk, public = published, True
    elif rel_apk:
        apk = rel_apk
        warns.append(f"the release's APK ({rel_apk[:12]}) is served behind the dashboard login: "
                     "no ROOK_PUBLIC_APK_SHA256 approves it")
    beta = ""
    if beta_gate:
        cands = [_sha_of(upd / "apk-beta") if upd else "", _sha_of(tree / BETA)]
        if beta_gate in cands:
            beta = beta_gate
        else:
            probs.append(f"ROOK_BETA_APK_SHA256 ({beta_gate[:12]}) approves no beta APK "
                         f"(updates dir: {cands[0][:12] or 'none'}, release: "
                         f"{cands[1][:12] or 'none'}): the beta channel would fall back to stable")
    worker = None
    if "worker" in tree_kinds(tree):
        m = _read_json(tree / REMOTE / PYZ_MANIFEST) or {}
        worker = {"build": m.get("build"), "version": m.get("version"), "sha256": m.get("sha256")}
    return {"problems": probs, "warnings": warns, "apk": apk, "apk_public": public,
            "apk_beta": beta, "worker": worker}


# -- after the restart: what the dashboard really serves -------------------------------------

def _get(url: str, method: str = "GET", timeout: float = 30):
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            body = r.read() if method == "GET" else b""
            return r.status, body, dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, b"", {}
    except (urllib.error.URLError, OSError) as e:
        return 0, str(getattr(e, "reason", e)).encode(), {}


def served_problems(base: str, want: dict, log) -> list[str]:
    """Ask the dashboard at ``base`` for each feed file and compare with
    ``want`` (from :func:`expected`). Returns what is dark or wrong."""
    import hashlib
    base = base.rstrip("/")
    probs, seen = [], []
    if want.get("worker"):
        st, body, _ = _get(f"{base}/{PYZ_MANIFEST}")
        try:
            m = json.loads(body) if st == 200 else None
        except ValueError:
            m = None
        if not isinstance(m, dict) or not isinstance(m.get("build"), int):
            probs.append(f"/{PYZ_MANIFEST} answered {st}")
        else:
            seen.append(f"/{PYZ_MANIFEST} build {m['build']}")
        st, body, _ = _get(f"{base}/{PYZ}")
        got = hashlib.sha256(body).hexdigest() if st == 200 else ""
        if got != want["worker"]["sha256"]:
            probs.append(f"/{PYZ} answered {st} {got[:12]}, expected "
                         f"{str(want['worker']['sha256'])[:12]} (build {want['worker']['build']})")
        else:
            seen.append(f"/{PYZ} build {want['worker']['build']} ({got[:12]})")
    stable = None
    if want.get("apk") and want.get("apk_public"):
        st, body, _ = _get(f"{base}/apk.json")
        try:
            stable = json.loads(body) if st == 200 else None
        except ValueError:
            stable = None
        if not isinstance(stable, dict) or stable.get("sha256") != want["apk"]:
            probs.append(f"/apk.json answered {st}, expected sha256 {want['apk'][:12]}")
            stable = None
        else:
            st, _, hdr = _get(f"{base}/apk", method="HEAD")
            if st != 200 or str(hdr.get("Content-Length", stable.get("size"))) != str(stable.get("size")):
                probs.append(f"/apk answered {st}")
            else:
                seen.append(f"/apk.json {stable.get('version_name')} ({want['apk'][:12]})")
    elif want.get("apk"):
        seen.append("/apk behind the login (not checked)")
    if want.get("apk_beta"):
        st, body, _ = _get(f"{base}/apk.json?channel=beta")
        try:
            m = json.loads(body) if st == 200 else None
        except ValueError:
            m = None
        ok = {want["apk_beta"]} | ({stable["sha256"]} if stable else set())
        if not isinstance(m, dict) or m.get("sha256") not in ok:
            probs.append(f"/apk.json?channel=beta answered {st}, expected sha256 "
                         f"{want['apk_beta'][:12]}")
        else:
            seen.append(f"beta {m.get('version_name')} ({str(m.get('sha256'))[:12]})")
    if seen:
        log("feed served: " + "; ".join(seen))
    return probs
