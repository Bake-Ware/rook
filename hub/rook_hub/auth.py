"""Users and tokens: the token half of T4.

Users own workers (W6) and tokens (T5). Every token is a JWT (T1), signed by
the hub with HS256, and also a row in the `tokens` table (K6), so revocation
and one use are the hub's record, not the claims alone.

The claims are standard JWT plus two of Rook's own (T2):

    jti      token GUID          sub   owner user GUID
    iat      minted at           exp   expires at (only if the purpose has a lifetime)
    purpose  what it is for      scope space-separated scopes it may be used as

A scope names the mechanism a token is for, nothing more (F8): `client`
acts through the hub (calls, minting, approving); `join` is an unattended
join (R3). Lifetime, one use and scopes come from the hub's stored defaults
per purpose, never from whoever mints (T2).
"""

import base64
import hashlib
import hmac
import json
import os
import uuid

from .errors import HubError

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    guid       TEXT PRIMARY KEY,
    moniker    TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
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

    def create_user(self, name):
        """A new user, with a GUID and a three-word moniker (G1)."""
        guid = str(uuid.uuid4())
        with self.hub._lock, self.db:
            moniker = self.hub._new_moniker(guid)
            self.db.execute("INSERT INTO users (guid, moniker, name, created_at) VALUES (?, ?, ?, ?)",
                            (guid, moniker, name, self.hub.clock()))
        return self.get_user(guid)

    def _user(self, user):
        row = self.db.execute("SELECT * FROM users WHERE guid = ? OR moniker = ?", (user, user)).fetchone()
        if row is None:
            raise HubError("unknown_user", f"No user is called {user!r}.",
                           "List the users and use a GUID or moniker from there.")
        return row

    def get_user(self, user):
        with self.hub._lock:
            return dict(self._user(user))

    def list_users(self):
        with self.hub._lock:
            return [dict(row) for row in self.db.execute("SELECT * FROM users ORDER BY created_at")]

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
        name = row["moniker"]
        if row["revoked_at"] is not None:
            raise HubError("token_revoked", f"Token {name} was revoked.", _TRY_AGAIN)
        if row["spent_at"] is not None:
            raise HubError("token_spent", f"Token {name} was already used, and it works once.",
                           "Mint a new join token for the next worker.")
        if row["expires_at"] is not None and self.hub.clock() >= row["expires_at"]:
            raise HubError("token_expired", f"Token {name} expired.", _TRY_AGAIN)
        if scope is not None and scope not in row["scope"].split():
            raise HubError("token_scope", f"Token {name} is a {row['purpose']} token and cannot be used for this.",
                           f"Use a token with the {scope} scope.")
        return row

    def verify(self, jwt, scope=None):
        """The token's record if it is good (optionally: carries `scope`), else HubError."""
        with self.hub._lock:
            return self._record(self._check(jwt, scope))

    def use(self, jwt, scope):
        """Check a token and record the use (T6); spend it if it works once. The lock is held."""
        row = self._check(jwt, scope)
        now = self.hub.clock()
        self.db.execute("UPDATE tokens SET last_used = ?, spent_at = CASE WHEN once THEN ? END WHERE guid = ?",
                        (now, now, row["guid"]))
        return row["owner"], row["guid"]

    def actor(self, user, token):
        """Who is acting: (user GUID, token GUID or None). Exactly one of `user`
        (a person acting directly) or `token` (a client token) is given. The lock is held."""
        if (user is None) == (token is None):
            raise HubError("no_actor", "Say who is acting: a user, or a token.",
                           "Pass user= for a person, or token= for an agent.")
        if token is not None:
            return self.use(token, "client")
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
