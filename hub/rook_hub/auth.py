"""Users and tokens: both front doors of T4.

Users own workers (W6) and tokens (T5). A person logs in with their name and
password (T4). The hub keeps only a salted scrypt hash of each password, and
never puts a password in the journal, an error, a log line or a return value.

 Every token is a JWT (T1), signed by
the hub with HS256, and also a row in the `tokens` table (K6), so revocation
and one use are the hub's record, not the claims alone.

The claims are standard JWT plus two of Rook's own (T2):

    jti      token GUID          sub   owner user GUID
    iat      minted at           exp   expires at (only if the purpose has a lifetime)
    purpose  what it is for      scope space-separated scopes it may be used as

A scope names the mechanism a token is for, nothing more (F8): `client`
acts through the hub (calls, minting, approving); `join` is an unattended
join (R3); `webhook` posts to the hub's webhook URL (J2); `session` is a
person logged in to the site (T4), kept in a cookie they never see (T3).
Lifetime, one use and scopes come from the hub's stored defaults per purpose,
never from whoever mints (T2).
"""

import base64
import hashlib
import hmac
import json
import os
import unicodedata
import uuid

from .errors import HubError

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    guid       TEXT PRIMARY KEY,
    moniker    TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    name_key   TEXT NOT NULL UNIQUE,   -- the name as login compares it (_name_key)
    password   TEXT NOT NULL,          -- salted scrypt hash, never the password
    created_at REAL NOT NULL,
    revoked_at REAL
);
CREATE TABLE IF NOT EXISTS token_defaults (
    purpose  TEXT PRIMARY KEY,
    scope    TEXT NOT NULL,          -- space-separated
    lifetime INTEGER,                -- seconds; NULL = no expiry
    once     INTEGER NOT NULL        -- 1 = spent by its first use
);
INSERT OR IGNORE INTO token_defaults VALUES ('agent', 'client', NULL, 0);
INSERT OR IGNORE INTO token_defaults VALUES ('join', 'join', 600, 1);
INSERT OR IGNORE INTO token_defaults VALUES ('webhook', 'webhook', NULL, 0);
INSERT OR IGNORE INTO token_defaults VALUES ('session', 'session', NULL, 0);
CREATE TABLE IF NOT EXISTS tokens (
    guid       TEXT PRIMARY KEY,     -- the jti claim
    moniker    TEXT NOT NULL UNIQUE,
    owner      TEXT NOT NULL REFERENCES users(guid),
    minted_by  TEXT REFERENCES tokens(guid),   -- NULL when the user minted it directly
    purpose    TEXT NOT NULL,
    scope      TEXT NOT NULL,
    once       INTEGER NOT NULL,
    minted_at  REAL NOT NULL,
    expires_at REAL,
    last_used  REAL,
    spent_at   REAL,
    revoked_at REAL
);
CREATE INDEX IF NOT EXISTS tokens_owner ON tokens (owner);
"""

# What each token failure tells the caller to try next (C3).
_TRY_AGAIN = "Ask the user it belongs to, or an agent of theirs, to mint a new token."


def _name_key(name):
    """How names are compared, at login and for uniqueness: Unicode NFKC, then case-folded."""
    return unicodedata.normalize("NFKC", name).casefold()


_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}


def _hash_password(password):
    """`scrypt$n$r$p$salt$hash`, with a fresh random salt each time."""
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${salt.hex()}${digest.hex()}"


def _password_matches(password, stored):
    _, n, r, p, salt, digest = stored.split("$")
    got = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p))
    return hmac.compare_digest(got, bytes.fromhex(digest))


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Auth:
    """Users and tokens for one hub. Shares the hub's database, lock, clock and journal."""

    def __init__(self, hub):
        self.hub = hub
        self.db = hub._db
        self.db.executescript(SCHEMA)
        self._secret = self._load_secret()

    def _load_secret(self):
        path = self.hub.state_dir / "token.secret"
        if not path.exists():
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(os.urandom(32).hex())
        return bytes.fromhex(path.read_text().strip())

    # ---------------------------------------------------------------- users

    def create_user(self, name, password):
        """A new user with a password (T4), a GUID and a three-word moniker (G1).
        Names identify users at login, so no two users share one."""
        guid, hashed = str(uuid.uuid4()), _hash_password(password)
        with self.hub._lock, self.db:
            if self.db.execute("SELECT 1 FROM users WHERE name_key = ?", (_name_key(name),)).fetchone():
                raise HubError("name_taken", f"A user is already called {name!r}.",
                               "Choose a different name for the new user.")
            moniker = self.hub._new_moniker(guid)
            self.db.execute(
                "INSERT INTO users (guid, moniker, name, name_key, password, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (guid, moniker, name, _name_key(name), hashed, self.hub.clock()))
        return self.get_user(guid)

    def login(self, name, password):
        """The user with this name and password (T4), or HubError. A wrong password
        and a name nobody has get the same error. A login against a real user is
        journaled under that user, whether it worked or not (L1)."""
        with self.hub._lock, self.db:
            row = self.db.execute("SELECT * FROM users WHERE name_key = ?", (_name_key(name),)).fetchone()
            if row is not None:
                if not _password_matches(password, row["password"]):
                    outcome = "wrong_password"
                elif row["revoked_at"] is not None:
                    outcome = "user_revoked"
                else:
                    outcome = "ok"
                self.hub._journal("auth", (row["guid"], None), None, {"login": outcome})
        if row is None or outcome == "wrong_password":
            raise HubError("login_failed", "No user has that name and password.",
                           "Check the name and the password, and try again.")
        if outcome == "user_revoked":
            raise HubError("user_revoked", f"User {row['moniker']} was revoked and cannot log in.",
                           "Ask someone on this hub to create a new user for you.")
        return self._user_record(row)

    def change_password(self, target, password, *, user=None, token=None):
        """Set a user's password (T4). Journaled, without the password (L1)."""
        hashed = _hash_password(password)
        with self.hub._lock, self.db:
            actor = self.actor(user, token)
            row = self._user(target)
            self.db.execute("UPDATE users SET password = ? WHERE guid = ?", (hashed, row["guid"]))
            self.hub._journal("auth", actor, None, {"password_changed": row["guid"]})

    def _user_record(self, row):
        """A user as the hub hands it out: never the password hash."""
        return {name: row[name] for name in ("guid", "moniker", "name", "created_at", "revoked_at")}

    def _user(self, user):
        row = self.db.execute("SELECT * FROM users WHERE guid = ? OR moniker = ?", (user, user)).fetchone()
        if row is None:
            raise HubError("unknown_user", f"No user is called {user!r}.",
                           "List the users and use a GUID or moniker from there.")
        return row

    def get_user(self, user):
        with self.hub._lock:
            return self._user_record(self._user(user))

    def list_users(self):
        with self.hub._lock:
            return [self._user_record(row) for row in self.db.execute("SELECT * FROM users ORDER BY created_at")]

    def revoke_user(self, target, *, user=None, token=None):
        """Revoke a user and every token that chains back to them (T5)."""
        with self.hub._lock, self.db:
            actor = self.actor(user, token)
            row = self._user(target)
            now = self.hub.clock()
            revoked = [r[0] for r in self.db.execute(
                "SELECT guid FROM tokens WHERE owner = ? AND revoked_at IS NULL ORDER BY minted_at", (row["guid"],))]
            self.db.execute("UPDATE tokens SET revoked_at = ? WHERE owner = ? AND revoked_at IS NULL",
                            (now, row["guid"]))
            self.db.execute("UPDATE users SET revoked_at = ? WHERE guid = ? AND revoked_at IS NULL",
                            (now, row["guid"]))
            self.hub._journal("auth", actor, None, {"revoked_user": row["guid"], "revoked_tokens": revoked})

    # --------------------------------------------------------------- tokens

    def mint(self, purpose, *, user=None, token=None):
        """Mint a token for `purpose`, owned by the acting user: the user who
        mints directly, or the owner of the token that mints (T5). Lifetime,
        scope and one use come from the hub's defaults (T2). Returns the JWT."""
        with self.hub._lock, self.db:
            actor = self.actor(user, token)
            default = self.db.execute("SELECT * FROM token_defaults WHERE purpose = ?", (purpose,)).fetchone()
            if default is None:
                known = ", ".join(r[0] for r in self.db.execute("SELECT purpose FROM token_defaults ORDER BY purpose"))
                raise HubError("unknown_purpose", f"This hub mints no {purpose!r} tokens.",
                               f"Mint one of: {known}.")
            guid, now = str(uuid.uuid4()), self.hub.clock()
            expires = None if default["lifetime"] is None else now + default["lifetime"]
            self.db.execute(
                "INSERT INTO tokens (guid, moniker, owner, minted_by, purpose, scope, once, minted_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (guid, self.hub._new_moniker(guid), actor[0], actor[1], purpose, default["scope"],
                 default["once"], now, expires))
            self.hub._journal("auth", actor, None, {"minted": guid, "purpose": purpose})
        claims = {"jti": guid, "sub": actor[0], "iat": int(now), "purpose": purpose, "scope": default["scope"]}
        if expires is not None:
            claims["exp"] = int(expires)
        return self._sign(claims)

    def _sign(self, claims):
        header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
        payload = _b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
        signature = hmac.new(self._secret, f"{header}.{payload}".encode(), hashlib.sha256).digest()
        return f"{header}.{payload}.{_b64(signature)}"

    def _check(self, jwt, scope=None):
        """The token's row if the JWT is good, else HubError. The lock is held.
        A check is not a use: it leaves last_used alone."""
        row = self._row(jwt)
        refusal = self._refusal(row, scope)
        if refusal is not None:
            raise refusal
        return row

    def _row(self, jwt):
        """The row of a JWT this hub minted, else HubError token_invalid. The lock is held."""
        invalid = HubError("token_invalid", "That is not a token this hub minted.", _TRY_AGAIN)
        try:
            header, payload, signature = jwt.split(".")
            expected = hmac.new(self._secret, f"{header}.{payload}".encode(), hashlib.sha256).digest()
            if not hmac.compare_digest(_unb64(signature), expected):
                raise invalid
            if json.loads(_unb64(header)) != {"alg": "HS256", "typ": "JWT"}:
                raise invalid
            claims = json.loads(_unb64(payload))
            if not isinstance(claims, dict):
                raise invalid
        except (ValueError, AttributeError, TypeError):
            raise invalid from None
        row = self.db.execute("SELECT * FROM tokens WHERE guid = ?", (claims.get("jti"),)).fetchone()
        if row is None:
            raise invalid
        return row

    def _refusal(self, row, scope):
        """Why a recognised token cannot be used now (a HubError), or None."""
        name = row["moniker"]
        if row["revoked_at"] is not None:
            return HubError("token_revoked", f"Token {name} was revoked.", _TRY_AGAIN)
        if row["spent_at"] is not None:
            return HubError("token_spent", f"Token {name} was already used, and it works once.",
                            "Mint a new join token for the next worker.")
        if row["expires_at"] is not None and self.hub.clock() >= row["expires_at"]:
            return HubError("token_expired", f"Token {name} expired.", _TRY_AGAIN)
        wanted = (scope,) if isinstance(scope, str) else scope    # one scope, or any of several
        if wanted is not None and not set(wanted) & set(row["scope"].split()):
            return HubError("token_scope", f"Token {name} is a {row['purpose']} token and cannot be used for this.",
                            f"Use a token with the {' or '.join(wanted)} scope.")
        return None

    def verify(self, jwt, scope=None):
        """The token's record if it is good (optionally: carries `scope`), else HubError."""
        with self.hub._lock:
            return self._record(self._check(jwt, scope))

    def use(self, jwt, scope):
        """Check a token and record the use (T6); spend it if it works once. The lock is held.

        A recognised token that is refused (revoked, spent, expired, wrong scope)
        is journaled under its owner with the reason (L1), and committed at once,
        so the entry stays when the caller's transaction rolls back. Every caller
        uses the token before it writes anything, so nothing else is committed early.
        A token the hub does not recognise is not journaled: there is no one to attribute it to."""
        row = self._row(jwt)
        refusal = self._refusal(row, scope)
        if refusal is not None:
            self.hub._journal("auth", (row["owner"], row["guid"]), None, {"refused": refusal.message["code"]})
            self.db.commit()
            raise refusal
        now = self.hub.clock()
        self.db.execute("UPDATE tokens SET last_used = ?, spent_at = CASE WHEN once THEN ? END WHERE guid = ?",
                        (now, now, row["guid"]))
        self.hub.events.emit("token.used", {"token": row["guid"], "token_moniker": row["moniker"], "user": row["owner"],
                                            "purpose": row["purpose"]}, user=row["owner"], token=row["guid"],
                             journal=False)  # on the bus for jobs; not journaled each time (see Needs Bake)
        return row["owner"], row["guid"]

    def actor(self, user, token):
        """Who is acting: (user GUID, token GUID or None). Exactly one of `user`
        (a person acting directly) or `token` (a client token, or a site session) is given.
        The lock is held."""
        if (user is None) == (token is None):
            raise HubError("no_actor", "Say who is acting: a user, or a token.",
                           "Pass user= for a person, or token= for an agent.")
        if token is not None:
            return self.use(token, ("client", "session"))
        row = self._user(user)
        if row["revoked_at"] is not None:
            raise HubError("user_revoked", f"User {row['moniker']} was revoked.",
                           "Act as a user who has not been revoked.")
        return row["guid"], None

    def revoke_token(self, target, *, user=None, token=None):
        """Revoke one token by GUID or moniker. It stops verifying at once."""
        with self.hub._lock, self.db:
            actor = self.actor(user, token)
            row = self.db.execute("SELECT guid FROM tokens WHERE guid = ? OR moniker = ?",
                                  (target, target)).fetchone()
            if row is None:
                raise HubError("unknown_token", f"No token is called {target!r}.",
                               "List the tokens and use a GUID or moniker from there.")
            self.db.execute("UPDATE tokens SET revoked_at = ? WHERE guid = ? AND revoked_at IS NULL",
                            (self.hub.clock(), row["guid"]))
            self.hub._journal("auth", actor, None, {"revoked_token": row["guid"]})

    def _record(self, row):
        return {"guid": row["guid"], "moniker": row["moniker"], "owner": row["owner"],
                "owner_moniker": self._user(row["owner"])["moniker"], "minted_by": row["minted_by"],
                "purpose": row["purpose"], "scopes": row["scope"].split(), "minted_at": row["minted_at"],
                "expires_at": row["expires_at"], "last_used": row["last_used"],
                "spent_at": row["spent_at"], "revoked_at": row["revoked_at"]}

    def list_tokens(self):
        """Every token with its scopes, owner and last use (T6)."""
        with self.hub._lock:
            return [self._record(row) for row in self.db.execute("SELECT * FROM tokens ORDER BY minted_at")]
