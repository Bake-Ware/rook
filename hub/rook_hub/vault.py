"""The vault (X1 to X6): secrets kept encrypted at the hub, used by reference.

A secret is a scope plus a name (X1). The scope is a worker, a band, the hub,
or a plain string. Each secret is a hub object with a GUID and a moniker (G1).
Values are encrypted with a vault key the hub is given when it starts (X2, X4);
the key is never written to the hub's data directory. Without it the vault is
locked: every secret operation says so, and the rest of the hub works.

For the scrubber (L4) each value also has a keyed hash, and a keyed hash of its
first few characters, kept in memory as an index, so writing a journal entry
never decrypts anything (X6). Workers hold no vault (X5).
"""

import hashlib
import re
import uuid

from nacl.exceptions import CryptoError
from nacl.secret import SecretBox

from .errors import HubError
from .journal import iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS secrets (
    guid       TEXT PRIMARY KEY,
    moniker    TEXT NOT NULL UNIQUE,
    kind       TEXT NOT NULL,          -- worker, band, hub or string
    scope      TEXT NOT NULL,          -- the worker's or band's GUID, "hub", or the plain string
    name       TEXT NOT NULL,
    value      BLOB NOT NULL,          -- encrypted with the vault key
    head       TEXT NOT NULL,          -- keyed hash of the value's first characters (the scrubber's index)
    hash       TEXT NOT NULL,          -- keyed hash of the whole value
    length     INTEGER NOT NULL,       -- the value's length in characters
    user       TEXT,                   -- who made it
    token      TEXT,
    created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS secrets_scope_name ON secrets (kind, scope, name);
CREATE INDEX IF NOT EXISTS secrets_name ON secrets (name);
CREATE TABLE IF NOT EXISTS vault_check (
    id    INTEGER PRIMARY KEY CHECK (id = 1),
    check_value BLOB NOT NULL          -- a known text sealed with the key: tells a wrong key from a right one
);
"""

NAME = re.compile(r"[A-Za-z0-9._-]+\Z")
REFERENCE = re.compile(r"secret:([A-Za-z0-9._-]+)\Z")   # a whole string, nothing around it (X2)
HEAD = 8          # characters the scrubber's index looks at first; shorter values are not scrubbed
_CHECK = b"rook vault check"
_LOCKED = ("vault_locked", "The vault is locked: this hub was started without its vault key.",
           "Start the hub with its vault key. Calls that use no secret work as before.")
_WRONG = ("vault_key_wrong", "The vault key this hub was started with is not the key its secrets were sealed with.",
          "Start the hub again with the right vault key.")


def _subkey(key, purpose):
    return hashlib.blake2b(key, digest_size=32, person=purpose).digest()


class Vault:
    def __init__(self, hub, key):
        self.hub = hub
        self.state, self._box, self._hash_key = "locked", None, None
        self.index = {}   # keyed hash of a value's first HEAD characters -> [(length, hash, name)]
        if key is None:
            return
        if not isinstance(key, bytes) or len(key) != 32:
            raise ValueError("the vault key is 32 bytes")
        box = SecretBox(_subkey(key, b"rook-vault-box"))
        with hub._lock, hub._db:
            row = hub._db.execute("SELECT check_value FROM vault_check").fetchone()
            if row is None:
                hub._db.execute("INSERT INTO vault_check (id, check_value) VALUES (1, ?)", (box.encrypt(_CHECK),))
            else:
                try:
                    if box.decrypt(row[0]) != _CHECK:
                        raise CryptoError
                except CryptoError:
                    self.state = "wrong_key"
                    return
        self.state, self._box, self._hash_key = "open", box, _subkey(key, b"rook-scrub-hash")
        with hub._lock:
            for row in hub._db.execute("SELECT name, head, hash, length FROM secrets WHERE length >= ?", (HEAD,)):
                self.index.setdefault(row["head"], []).append((row["length"], row["hash"], row["name"]))

    def _open(self):
        if self.state == "locked":
            raise HubError(*_LOCKED)
        if self.state == "wrong_key":
            raise HubError(*_WRONG)

    def keyed_hash(self, text):
        """A hash only this vault's key can make, so the stored hashes say nothing without it."""
        return hashlib.blake2b(text.encode("utf-8"), key=self._hash_key, digest_size=16).hexdigest()

    # ------------------------------------------------------------ scopes

    def _scope(self, scope):
        """("worker" | "band" | "hub" | "string", stored scope) from "hub",
        "worker:<moniker or GUID>", "band:<moniker or GUID>" or any other string. Lock held."""
        if not isinstance(scope, str) or not scope:
            raise HubError("bad_message", "args.scope is required and must be a string.",
                           "Give scope as \"hub\", \"worker:<moniker>\", \"band:<moniker>\" or a plain string.")
        if scope == "hub":
            return "hub", "hub"
        if scope.startswith("worker:"):
            return "worker", self.hub._resolve(scope[7:])["guid"]
        if scope.startswith("band:"):
            from .bands import _band
            return "band", _band(self.hub, scope[5:])["guid"]
        return "string", scope

    def _show_scope(self, kind, scope):
        return {"hub": "hub", "string": scope}.get(kind) or f"{kind}:{self.hub._moniker(scope)}"

    # ------------------------------------------------------- hub caps (X3)

    def create(self, args, actor):
        """A new secret. Its value is encrypted and never returned or journaled."""
        name, value = args.get("name"), args.get("value")
        if not isinstance(name, str) or not NAME.match(name):
            raise HubError("bad_message", "args.name is required: letters, digits, dot, dash and underscore.",
                           "Give a name like deploy-key; calls refer to it as secret:deploy-key.")
        if not isinstance(value, str) or not value:
            raise HubError("bad_message", "args.value is required and must be a string.", "Give the value as text.")
        self._open()
        guid = str(uuid.uuid4())
        with self.hub._lock, self.hub._db:
            user, token = self.hub.auth.actor(**actor)
            kind, scope = self._scope(args.get("scope"))
            shown = self._show_scope(kind, scope)
            if self.hub._db.execute("SELECT 1 FROM secrets WHERE kind = ? AND scope = ? AND name = ?",
                                    (kind, scope, name)).fetchone():
                raise HubError("secret_exists", f"{shown} already has a secret called {name}.",
                               "Choose another name; secrets are not replaced.")
            head, full = self.keyed_hash(value[:HEAD]), self.keyed_hash(value)
            self.hub._db.execute(
                "INSERT INTO secrets (guid, moniker, kind, scope, name, value, head, hash, length, user, token, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (guid, self.hub._new_moniker(guid), kind, scope, name, self._box.encrypt(value.encode("utf-8")),
                 head, full, len(value), user, token, self.hub.clock()))
            if len(value) >= HEAD:
                self.index.setdefault(head, []).append((len(value), full, name))
        return {"created": self._record(guid)}

    def _record(self, guid):
        row = self.hub._db.execute("SELECT * FROM secrets WHERE guid = ?", (guid,)).fetchone()
        return {"guid": row["guid"], "moniker": row["moniker"], "scope": self._show_scope(row["kind"], row["scope"]),
                "name": row["name"], "reference": f"secret:{row['name']}",
                "created_by": self.hub._moniker(row["user"]) if row["user"] else None,
                "token": self.hub._moniker(row["token"]) if row["token"] else None,
                "created_at": iso(row["created_at"])}

    def list(self, args, actor):
        """Every secret's scope, name and moniker, who made it and when; never a value."""
        self._open()
        with self.hub._lock:
            self.hub.auth.actor(**actor)
            return {"secrets": [self._record(row[0]) for row in
                                self.hub._db.execute("SELECT guid FROM secrets ORDER BY kind, scope, name")]}

    # ------------------------------------------------------ reveal (X3)

    def reveal(self, secret, *, user=None, token=None):
        """One secret's value, to a user acting directly. A token is refused. Journaled without the value."""
        if token is not None:
            raise HubError("reveal_needs_a_person", "Only a person can view a secret's value.",
                           "Use the secret by reference (secret:<name>) in a call; a person can reveal it on the Secrets page.")
        self._open()
        with self.hub._lock, self.hub._db:
            actor = self.hub.auth.actor(user, None)
            row = self.hub._db.execute("SELECT * FROM secrets WHERE guid = ? OR moniker = ?", (secret, secret)).fetchone()
            if row is None:
                raise HubError("unknown_secret", f"No secret is called {secret!r}.",
                               "List the secrets and use a moniker or GUID from there.")
            self.hub._journal("auth", actor, None, {"revealed": row["guid"], "name": row["name"],
                                                    "scope": self._show_scope(row["kind"], row["scope"])})
            return self._box.decrypt(row["value"]).decode("utf-8")

    # --------------------------------------------------- references (X2)

    def substitute(self, args, worker):
        """`args` with every string that is exactly secret:<name> replaced by its value,
        found in the worker's scope, then its band's, then the hub's (X1); for the hub's
        own caps (worker "hub") only in the hub's scope. Raises HubError if a name is
        found nowhere, or if the vault cannot be opened."""
        names = set()

        def find(value):
            if isinstance(value, str):
                match = REFERENCE.match(value)
                if match:
                    names.add(match.group(1))
            elif isinstance(value, dict):
                for item in value.values():
                    find(item)
            elif isinstance(value, list):
                for item in value:
                    find(item)
        find(args)
        if not names:
            return args
        self._open()
        scopes = [("hub", "hub")]
        if worker != "hub":
            from . import bands
            band = bands.of_worker(self.hub, worker)
            scopes = [("worker", worker)] + ([("band", band["guid"])] if band else []) + scopes
        values = {}
        with self.hub._lock:
            for name in names:
                for kind, scope in scopes:
                    row = self.hub._db.execute("SELECT value FROM secrets WHERE kind = ? AND scope = ? AND name = ?",
                                               (kind, scope, name)).fetchone()
                    if row is not None:
                        values[name] = self._box.decrypt(row[0]).decode("utf-8")
                        break
                else:
                    searched = ", ".join(self._show_scope(k, s) for k, s in scopes)
                    raise HubError("unknown_secret", f"No secret called {name} in {searched}.",
                                   "Create it with the hub cap secret.create in one of those scopes, "
                                   "or check the name with secret.list.")

        def put(value):
            if isinstance(value, str):
                match = REFERENCE.match(value)
                return values[match.group(1)] if match else value
            if isinstance(value, dict):
                return {k: put(v) for k, v in value.items()}
            if isinstance(value, list):
                return [put(v) for v in value]
            return value
        return put(args)


def caps():
    """The vault's hub caps (X3); reveal is not one of them."""
    scope = {"type": "string", "description": "\"hub\", \"worker:<moniker or GUID>\", \"band:<moniker or GUID>\", "
                                              "or any other text for a plain scope."}
    return {
        "secret.create": (lambda hub, args, actor: hub.vault.create(args, actor), {
            "description": "Keep a secret in the hub's vault. Use it in any call's arguments as the whole string "
                           "secret:<name>; the hub puts the value in as the call goes out, looking in the worker's "
                           "scope, then its band's, then the hub's. The value is never shown or journaled.",
            "schema": {"type": "object", "required": ["scope", "name", "value"], "additionalProperties": False,
                       "properties": {"scope": scope, "name": {"type": "string"}, "value": {"type": "string"}}},
        }),
        "secret.list": (lambda hub, args, actor: hub.vault.list(args, actor), {
            "description": "List the vault's secrets: scope, name, moniker, who made each and when. Never a value.",
            "schema": {"type": "object", "additionalProperties": False, "properties": {}},
        }),
    }
