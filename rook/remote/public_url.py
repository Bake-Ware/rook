"""The hub's public base URL and whether a request arrived over HTTPS.

One place decides ``http`` versus ``https`` for everything the dashboard
hands out (installer scripts, one-liners, the account origin used for CSRF
and OAuth, the update manifest URL) and for the ``Secure`` cookie flag.

The dashboard itself only ever listens on plain HTTP. Public deployments put
it behind a TLS proxy (a Cloudflare tunnel, nginx), so "served on HTTP" says
nothing about what the browser sees. The configured domain does:

* ``--domain``/``ROOK_DOMAIN`` (or the Settings page / setup wizard value)
  may carry a scheme: ``http://192.0.2.10:7005`` or ``https://rook.example.com``.
  That always wins.
* Without one, ``ROOK_PUBLIC_BASE`` (the build host's manifest base) supplies
  the scheme when it names the same host[:port].
* Otherwise the domain is ``https`` unless it points straight at this
  dashboard's own plain-HTTP port (``<lan-ip>:7005``, what
  ``scripts/local-hub.sh`` passes) or at port 80. A bare host name, the
  production shape, stays ``https`` exactly as before.
"""

from __future__ import annotations

import json
import os
import re
from typing import Mapping, Optional

_SCHEME = re.compile(r"(?i)^(https?)://")


def _port(hostport: str) -> Optional[int]:
    """The explicit port of ``host:port`` (IPv4 / name), else None."""
    if hostport.count(":") != 1:
        return None
    tail = hostport.rsplit(":", 1)[1]
    return int(tail) if tail.isdigit() else None


def split_domain(value: str, dashboard_port: Optional[int] = None,
                 environ: Optional[Mapping[str, str]] = None) -> tuple[str, str]:
    """``(scheme, host[:port])`` for a configured dashboard domain.

    ``value`` is ``host[:port]`` or ``http(s)://host[:port][/]``; a path is
    dropped (the dashboard is always served at the root)."""
    value = (value or "").strip()
    m = _SCHEME.match(value)
    if m:
        host = value[m.end():].split("/", 1)[0]
        return m.group(1).lower(), host
    host = value.rstrip("/")
    env = os.environ if environ is None else environ
    base = (env.get("ROOK_PUBLIC_BASE") or "").strip()
    bm = _SCHEME.match(base)
    if bm and base[bm.end():].split("/", 1)[0].lower() == host.lower():
        return bm.group(1).lower(), host
    port = _port(host)
    if port == 80 or (port is not None and port != 443 and port == dashboard_port):
        return "http", host
    return "https", host


def public_base(value: str, dashboard_port: Optional[int] = None,
                environ: Optional[Mapping[str, str]] = None) -> str:
    """``scheme://host[:port]`` with no trailing slash."""
    scheme, host = split_domain(value, dashboard_port, environ)
    return f"{scheme}://{host}"


def request_is_https(request) -> bool:
    """True when the browser reached us over HTTPS: a TLS socket, or a proxy
    saying so (``X-Forwarded-Proto``, ``Forwarded: proto=``, Cloudflare's
    ``CF-Visitor``).

    The forwarded headers are not authenticated, and they do not need to be:
    this only ever *adds* the ``Secure`` cookie flag (see :func:`cookie_secure`),
    so a forged header can only make the forger's own cookie stricter."""
    if getattr(request, "secure", False):
        return True
    headers = request.headers
    proto = headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().lower()
    if proto == "https":
        return True
    fwd = headers.get("Forwarded", "").split(",", 1)[0]
    if re.search(r'(?i)(?:^|;)\s*proto="?https"?\s*(?:;|$)', fwd):
        return True
    try:
        visitor = json.loads(headers.get("CF-Visitor", "") or "{}")
    except ValueError:
        visitor = {}
    return isinstance(visitor, dict) and visitor.get("scheme") == "https"


def cookie_secure(request, base: str) -> bool:
    """Whether a cookie set on this response must be ``Secure``.

    An ``https`` public base keeps every cookie ``Secure`` no matter how the
    request arrived (what every public deployment had before). Only a hub
    configured as ``http://`` (a LAN quickstart) sends cookies without it, and
    even then a request that came in over HTTPS still gets ``Secure``. An
    empty ``base`` means "no configured floor": Secure exactly when the
    request came over HTTPS."""
    return base.lower().startswith("https://") or request_is_https(request)


def set_cookie(response, request, base: str, name: str, value: str, **kw) -> None:
    """``response.set_cookie`` with ``secure`` decided by :func:`cookie_secure`
    and ``httponly``/``samesite=Lax`` by default. Every dashboard and account
    cookie goes through here."""
    kw.setdefault("httponly", True)
    kw.setdefault("samesite", "Lax")
    response.set_cookie(name, value, secure=cookie_secure(request, base), **kw)
