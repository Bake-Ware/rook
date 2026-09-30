"""Settings resolution, secret references and masking, shared by the hub and
workers (docs/design/settings.md, part 3).

A setting's effective value comes from the highest source that holds a valid
value::

    default < file < hub < band < worker < user < env

``file`` is a legacy file (``setup.json``): it seeds a key until a value is
stored for it. ``hub``/``band``/``worker``/``user`` are rows in the hub
settings store (the key's home scope plus the lower scopes it declares
``overridable``). ``env`` is the process environment or a command-line flag;
it wins and locks the field in the UI. Secrets never travel as values here: a secret layer
carries a vault reference and a fingerprint.

This module is stdlib-only: it ships inside the worker bundle.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable

#: Sources in increasing precedence.
SOURCES = ("default", "file", "hub", "band", "worker", "user", "env")
MASK = "***"

#: ``{{secret:<vault name>}}``: a reference to a vault entry, resolved where
#: the value is used (the hub for rook_call args; the worker at use for pushed
#: worker settings). The reference itself is not secret.
SECRET_REF = re.compile(r"^\{\{secret:([a-z0-9][a-z0-9._-]{0,63})\}\}$")
_VAULT_NAME_BAD = re.compile(r"[^a-z0-9._-]+")

#: Environment names that are treated as secret when nothing better is known.
_SECRETISH = re.compile(r"(PASS|PASSWORD|PASSWD|TOKEN|SECRET|PSK|KEY|CREDENTIAL|AUTH|COOKIE)",
                        re.IGNORECASE)


def fingerprint(value: Any) -> str:
    """First 8 hex of SHA-256: shows that a secret changed without revealing it."""
    if value in (None, ""):
        return ""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:8]


def secret_ref(value: Any) -> str | None:
    """The vault name if ``value`` is exactly a ``{{secret:name}}`` reference."""
    if not isinstance(value, str):
        return None
    m = SECRET_REF.match(value.strip())
    return m.group(1) if m else None


def make_ref(name: str) -> str:
    return "{{secret:" + name + "}}"


def vault_name(*parts: str) -> str:
    """A valid vault name built from ``parts`` (lower-cased, joined by dots,
    other characters replaced by ``-``, at most 64 characters)."""
    clean = [(_VAULT_NAME_BAD.sub("-", str(p).lower()).strip("-.") or "x") for p in parts]
    name = ".".join(clean)
    if not name[0].isalnum():
        name = "s" + name
    if len(name) > 64:
        name = name[:55] + "-" + hashlib.sha256(name.encode()).hexdigest()[:8]
    return name


def looks_secret(env_name: str) -> bool:
    return bool(_SECRETISH.search(env_name or ""))


def mask_env(env: dict | None, public: Iterable[str] = ()) -> dict:
    """Mask the values of an environment map. A value stays visible only when
    its name is in ``public`` (declared, non-secret setting variables) and does
    not look like a credential. ``{{secret:…}}`` references are kept: they name
    a vault entry, they are not the value."""
    if not isinstance(env, dict):
        return {}
    public = set(public)
    out = {}
    for k, v in env.items():
        k = str(k)
        if v is None or secret_ref(v) or (k in public and not looks_secret(k)):
            out[k] = v
        else:
            out[k] = MASK
    return out


def mask_worker_config(cfg: dict | None, public_env: Iterable[str] = ()) -> dict:
    """A worker ``config.json`` (or a config push) safe to return or journal:
    ``psk`` masked, ``env`` values masked per :func:`mask_env`."""
    if not isinstance(cfg, dict):
        return {}
    out = dict(cfg)
    if out.get("psk"):
        out["psk"] = MASK
    if "env" in out:
        out["env"] = mask_env(out.get("env"), public_env)
    return out


def _coerce(setting: Any, raw: Any) -> Any:
    if raw is None:
        return None
    return setting.coerce(raw)


def resolve(setting: Any, key: str, layers: Iterable[tuple] = (),
            env: tuple | None = None, file: Any = None) -> dict:
    """Resolve one setting and explain it.

    ``layers``: ``(source, value)`` or ``(source, value, extra)`` store rows,
    lowest precedence first (``hub``, ``band``, ``worker``, ``user``).
    ``env``: ``(variable, raw value)`` when the environment or a flag sets it.
    ``file``: the value a legacy file still supplies, if any.

    Returns ``{key, value, source, locked, env?, inherited, layers, invalid}``.
    ``value`` is the effective value (for secrets: ``"***"`` when set, never
    the value). ``inherited`` is what the value would be without the winning
    store layer (for "Reset to inherited"). Invalid values are skipped and
    listed, so one bad source degrades to the next instead of failing.
    """
    default = setting.default() if callable(setting.default) else setting.default
    chain: list[dict] = [{"source": "default", "value": None if setting.secret else default}]
    invalid: list[dict] = []
    if file is not None:
        chain.append({"source": "file", "value": file})
    for layer in layers:
        source, value = layer[0], layer[1]
        extra = layer[2] if len(layer) > 2 and isinstance(layer[2], dict) else {}
        chain.append({"source": source, "value": value, **extra})
    if env is not None:
        chain.append({"source": "env", "value": env[1], "env": env[0]})

    winner = chain[0]
    effective: Any = None if setting.secret else default
    for entry in reversed(chain):
        if entry["source"] == "default":
            break
        raw = entry["value"]
        if raw is None:
            continue
        if setting.secret:
            winner, effective = entry, raw
            break
        try:
            effective = _coerce(setting, raw)
        except (ValueError, TypeError) as e:
            invalid.append({"source": entry["source"], "error": str(e)})
            continue
        winner = entry
        break

    # What the value would be without the winning store layer.
    inherited: Any = None if setting.secret else default
    below = False
    for entry in reversed(chain):
        if entry is winner:
            below = True
            continue
        if not below or entry["source"] == "default":
            continue
        if entry["value"] is None:
            continue
        try:
            inherited = entry["value"] if setting.secret else _coerce(setting, entry["value"])
            break
        except (ValueError, TypeError):
            continue

    def shown(v: Any) -> Any:
        if not setting.secret or v is None:
            return v
        return v if secret_ref(v) else MASK

    out = {
        "key": key,
        "value": shown(effective),
        "source": winner["source"],
        "locked": winner["source"] == "env",
        "inherited": shown(inherited),
        "layers": [{**e, "value": shown(e["value"])} for e in chain[1:]],
        "invalid": invalid,
    }
    if winner["source"] == "env":
        out["env"] = winner.get("env")
    if setting.secret and effective is not None and not secret_ref(effective):
        out["fingerprint"] = fingerprint(effective)
    return out
