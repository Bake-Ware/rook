"""Shared band credentials and short-lived installer grants.

The dashboard and MCP service must use the same ROOK_ENROLLMENT_DB (and
ROOK_SETUP_PATH). SQLite transactions serialize redemption, rotation, and
attempt limits across processes. A pairing code grants ONE band's config;
it is never a dashboard login, MCP token, or the permanent PSK.
"""

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time

from . import setup_store
from .psk import generate_psk

PAIRING_TTL = 300
_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
_GLOBAL_ATTEMPTS = 20  # per minute, shared across all server processes
_PEER_ATTEMPTS = 5


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class JoinDenied(Exception):
    pass


class JoinLimited(JoinDenied):
    pass


class EnrollmentStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or os.environ.get("ROOK_ENROLLMENT_DB")
                         or setup_store.setup_path().with_name("enrollment.db")).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS bands (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, psk TEXT NOT NULL,
                    psk_hash TEXT UNIQUE NOT NULL, hub TEXT NOT NULL DEFAULT '',
                    is_primary INTEGER NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1, epoch INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS retired (
                    psk_hash TEXT PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS pairing (
                    band_id TEXT PRIMARY KEY, code TEXT UNIQUE NOT NULL,
                    epoch INTEGER NOT NULL, expires REAL NOT NULL, session TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    bucket TEXT PRIMARY KEY, window INTEGER NOT NULL, count INTEGER NOT NULL
                );
            """)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def register(self, name: str, psk: str, hub: str = "", *, primary=False) -> dict:
        if not isinstance(psk, str) or not psk or len(psk) > 1024:
            raise ValueError("a band PSK is required (at most 1024 characters)")
        digest = _hash(psk)
        with self._db() as db:
            if db.execute("SELECT 1 FROM retired WHERE psk_hash=?", (digest,)).fetchone():
                raise ValueError("this PSK was revoked; replace it on the Tokens page")
            row = db.execute("SELECT * FROM bands WHERE psk_hash=?", (digest,)).fetchone()
            if row:
                if not row["active"]:
                    raise ValueError("this band is revoked; replace its PSK on the Tokens page")
                if hub:
                    db.execute("UPDATE bands SET hub=? WHERE id=?", (hub, row["id"]))
                if primary:
                    db.execute("UPDATE bands SET is_primary=1 WHERE id=?", (row["id"],))
                return dict(db.execute("SELECT * FROM bands WHERE id=?", (row["id"],)).fetchone())
            uid = secrets.token_hex(16)
            db.execute("INSERT INTO bands(id,name,psk,psk_hash,hub,is_primary) VALUES(?,?,?,?,?,?)",
                       (uid, name or "band", psk, digest, hub, int(primary)))
            return dict(db.execute("SELECT * FROM bands WHERE id=?", (uid,)).fetchone())

    def import_config(self, fallback_psks=(), hub: str = "") -> None:
        """Import legacy config without ever resurrecting a retired key.

        Once rotated/revoked, the database is authoritative even if an old
        systemd environment or setup.json still contains the previous PSK.
        """
        config = setup_store.load()
        known = setup_store.load_bands()
        known_psks = {b["psk"] for b in known}
        known.extend({"name": "band", "psk": key} for key in fallback_psks
                     if key and key not in known_psks)
        for band in known:
            try:
                self.register(band["name"], band["psk"], config.get("hub_public") or hub,
                              primary=band["psk"] == config.get("band_psk"))
            except ValueError:
                pass  # retired keys remain retired across restarts

    def bands(self, *, active_only=False, secrets_visible=False) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM bands" + (" WHERE active=1" if active_only else "")
                              + " ORDER BY name,id").fetchall()
            out = []
            for row in rows:
                item = dict(row)
                item["label"] = item["psk_hash"][:8]
                if not secrets_visible:
                    item.pop("psk")
                item.pop("psk_hash")
                out.append(item)
            return out

    def issue(self, band_id: str, session: str | None = None) -> dict:
        now = time.time()
        with self._db() as db:
            band = db.execute("SELECT * FROM bands WHERE id=? AND active=1", (band_id,)).fetchone()
            if not band:
                raise ValueError("active band not found")
            row = db.execute("SELECT * FROM pairing WHERE band_id=?", (band_id,)).fetchone()
            if session is not None and (not row or row["session"] != session):
                raise ValueError("pairing was stopped; start a new pairing session")
            if row and row["expires"] > now and row["epoch"] == band["epoch"]:
                return dict(row)
            token = row["session"] if row else secrets.token_urlsafe(24)
            db.execute("DELETE FROM pairing WHERE band_id=?", (band_id,))
            for _ in range(20):
                code = "".join(secrets.choice(_ALPHABET) for _ in range(6))
                if row and code == row["code"]:
                    continue
                try:
                    db.execute("INSERT INTO pairing VALUES(?,?,?,?,?)",
                               (band_id, code, band["epoch"], now + PAIRING_TTL, token))
                    return {"band_id": band_id, "code": code,
                            "epoch": band["epoch"], "expires": now + PAIRING_TTL, "session": token}
                except sqlite3.IntegrityError:
                    continue
            raise RuntimeError("could not allocate a unique pairing code")

    def revoke_code(self, band_id: str) -> None:
        with self._db() as db:
            db.execute("DELETE FROM pairing WHERE band_id=?", (band_id,))

    def redeem(self, code: str, peer: str) -> dict:
        """Reusable until expiry/revocation; attempt counts commit even on denial.

        Never trust a caller-supplied forwarded IP header. A shared proxy may
        hit the per-peer limit sooner, but cannot bypass the global budget.
        """
        now = time.time()
        window = int(now // 60)
        result = None
        limited = False
        with self._db() as db:
            db.execute("DELETE FROM attempts WHERE window<>?", (window,))
            for bucket, limit in (("global", _GLOBAL_ATTEMPTS), ("peer:" + _hash(peer), _PEER_ATTEMPTS)):
                row = db.execute("SELECT count FROM attempts WHERE bucket=?", (bucket,)).fetchone()
                count = row["count"] if row else 0
                limited |= count >= limit
                db.execute("INSERT INTO attempts VALUES(?,?,?) ON CONFLICT(bucket) DO UPDATE SET count=excluded.count",
                           (bucket, window, min(count + 1, limit + 1)))
                if limited:
                    break
            if not limited and isinstance(code, str) and re.fullmatch(r"[a-z0-9]{6}", code):
                row = db.execute("""SELECT b.* FROM pairing p JOIN bands b ON b.id=p.band_id
                    WHERE p.code=? AND p.expires>? AND b.active=1 AND p.epoch=b.epoch""",
                                 (code, now)).fetchone()
                if row:
                    result = dict(row)
        if limited:
            raise JoinLimited("too many pairing attempts; retry in one minute")
        if result is None:
            raise JoinDenied("a valid, unexpired band pairing code is required")
        return result

    def rotate(self, band_id: str, psk: str = "") -> dict:
        psk = psk or generate_psk()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", psk):
            raise ValueError("use letters, numbers, hyphens, or underscores for the replacement PSK")
        digest = _hash(psk)
        with self._db() as db:
            band = db.execute("SELECT * FROM bands WHERE id=?", (band_id,)).fetchone()
            if not band:
                raise ValueError("band not found")
            if (db.execute("SELECT 1 FROM retired WHERE psk_hash=?", (digest,)).fetchone()
                    or db.execute("SELECT 1 FROM bands WHERE psk_hash=?", (digest,)).fetchone()):
                raise ValueError("choose a fresh PSK that has not already been used")
            db.execute("INSERT OR IGNORE INTO retired VALUES(?)", (band["psk_hash"],))
            db.execute("UPDATE bands SET psk=?,psk_hash=?,active=1,epoch=epoch+1 WHERE id=?",
                       (psk, digest, band_id))
            db.execute("DELETE FROM pairing WHERE band_id=?", (band_id,))
            return {"id": band_id, "psk": psk, "epoch": band["epoch"] + 1,
                    "label": digest[:8], "name": band["name"]}

    def revoke(self, band_id: str) -> None:
        with self._db() as db:
            band = db.execute("SELECT * FROM bands WHERE id=?", (band_id,)).fetchone()
            if not band:
                raise ValueError("band not found")
            db.execute("INSERT OR IGNORE INTO retired VALUES(?)", (band["psk_hash"],))
            db.execute("UPDATE bands SET active=0,epoch=epoch+1 WHERE id=?", (band_id,))
            db.execute("DELETE FROM pairing WHERE band_id=?", (band_id,))
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='devices'").fetchone():
                db.execute("UPDATE devices SET active=0 WHERE band_id=?", (band_id,))
