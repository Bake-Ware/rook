"""RSS/Atom feeds for the start page, fetched by the hub.

The browser never fetches a feed itself (no CORS, and the hub can cache):
it asks the hub, which fetches the URL with three limits.

* Address guard (SSRF): only ``http``/``https`` URLs without credentials,
  and the hub connects only to public addresses. The name is resolved here
  and the connection goes to the vetted address (a resolver that drops the
  rest), so a DNS answer cannot change between the check and the connect.
  Redirects are followed by hand, at most :data:`MAX_REDIRECTS`, each one
  checked the same way. The operator may let feeds reach private addresses
  (a homelab's own services) with the ``ui.feeds_allow_private`` setting;
  loopback, link-local and the rest stay refused unless that is on.
* Size: at most ``max_bytes`` of body, counted as it arrives.
* Time: one deadline for the whole fetch, redirects included.

Parsing is stdlib ElementTree on the size-capped bytes; a document that
declares entities is refused outright.
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import re
import socket
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver

MAX_REDIRECTS = 3
MAX_URL = 2000
MAX_ITEMS = 30
USER_AGENT = "Rook start page (feed reader)"
ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.9, */*;q=0.5"


class FeedError(Exception):
    """A plain-language reason the feed could not be shown."""


def address_ok(ip: str, allow_private: bool) -> bool:
    """Whether the hub may connect to ``ip`` for a feed."""
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if addr.is_unspecified or addr.is_multicast:
        return False
    if allow_private:
        return True
    return addr.is_global


def check_url(url: str) -> tuple[str, int]:
    """``(host, port)`` of a feed URL, or :class:`FeedError`."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL:
        raise FeedError("Enter a feed address (http or https).")
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        raise FeedError("That is not a valid web address.") from None
    if parts.scheme not in ("http", "https"):
        raise FeedError("Feeds must use http or https.")
    if parts.username is not None or parts.password is not None:
        raise FeedError("Feed addresses with a user name or password are not allowed.")
    host = (parts.hostname or "").rstrip(".")
    if not host:
        raise FeedError("That address has no host name.")
    return host, port or (443 if parts.scheme == "https" else 80)


class GuardedResolver(AbstractResolver):
    """Resolves a name and keeps only the addresses :func:`address_ok`
    allows; aiohttp then connects to those. (aiohttp skips the resolver for
    IP literals, so :func:`fetch` checks those itself.)"""

    def __init__(self, allow_private: bool) -> None:
        self.allow_private = allow_private

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_UNSPEC):
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM, family=family)
        except socket.gaierror:
            raise OSError(f"cannot resolve {host}") from None
        out = [{"hostname": host, "host": sockaddr[0], "port": port, "family": fam,
                "proto": proto, "flags": socket.AI_NUMERICHOST}
               for fam, _type, proto, _canon, sockaddr in infos
               if address_ok(sockaddr[0], self.allow_private)]
        if not out:
            raise BlockedAddress(host)
        return out

    async def close(self) -> None:
        return None


class BlockedAddress(OSError):
    def __init__(self, host: str) -> None:
        super().__init__(f"{host} is a private or local address")
        self.host = host


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


async def fetch(url: str, *, allow_private: bool = False, max_bytes: int = 512 * 1024,
                timeout: float = 8.0) -> bytes:
    """The body at ``url`` under the guard, size and time limits."""
    blocked = FeedError("That address is on a private or local network, which feeds may not "
                        "reach. The operator can allow it in Settings (Dashboard > Feeds).")
    connector = aiohttp.TCPConnector(resolver=GuardedResolver(allow_private), use_dns_cache=False,
                                     limit=2)
    deadline = aiohttp.ClientTimeout(total=timeout)
    try:
        async with aiohttp.ClientSession(connector=connector, timeout=deadline, trust_env=False,
                                         headers={"User-Agent": USER_AGENT, "Accept": ACCEPT}) as http:
            async with asyncio.timeout(timeout):
                for _hop in range(MAX_REDIRECTS + 1):
                    host, _port = check_url(url)
                    if _is_ip(host) and not address_ok(host.strip("[]"), allow_private):
                        raise blocked
                    async with http.get(url, allow_redirects=False) as resp:
                        if resp.status in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
                            url = urljoin(url, resp.headers["Location"])
                            continue
                        if resp.status != 200:
                            raise FeedError(f"The feed answered HTTP {resp.status}.")
                        if (resp.content_length or 0) > max_bytes:
                            raise FeedError(_too_big(max_bytes))
                        body = bytearray()
                        async for chunk in resp.content.iter_chunked(16384):
                            body += chunk
                            if len(body) > max_bytes:
                                raise FeedError(_too_big(max_bytes))
                        return bytes(body)
                raise FeedError("The feed redirected too many times.")
    except BlockedAddress:
        raise blocked from None
    except aiohttp.ClientConnectorError as e:
        if isinstance(getattr(e, "os_error", None), BlockedAddress):
            raise blocked from None
        raise FeedError("Could not connect to the feed's server.") from None
    except (TimeoutError, asyncio.TimeoutError):
        raise FeedError(f"The feed took longer than {timeout:g} seconds.") from None
    except aiohttp.ClientError:
        raise FeedError("The feed could not be downloaded.") from None


def _too_big(max_bytes: int) -> str:
    return f"The feed is larger than {max_bytes // 1024} KB, the limit on this hub."


# -- parsing -------------------------------------------------------------------

_TAG = re.compile(r"<[^>]{0,2000}>")
_SPACE = re.compile(r"\s+")


def _text(value: str | None, limit: int) -> str:
    if not value:
        return ""
    value = html.unescape(_TAG.sub(" ", value))
    value = _SPACE.sub(" ", value).strip()
    return value[: limit - 1] + "…" if len(value) > limit else value


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child(el, *names):
    for c in el:
        if _local(c.tag) in names:
            return c
    return None


def _child_text(el, *names) -> str:
    c = _child(el, *names)
    return (c.text or "") if c is not None else ""


def _link(el, base: str) -> str:
    """An item's web link: Atom ``<link rel=alternate href>`` or RSS
    ``<link>text</link>``; only http(s)."""
    best = ""
    for c in el:
        if _local(c.tag) != "link":
            continue
        href = c.get("href")
        if href is None:
            href = c.text or ""
        rel = c.get("rel", "alternate")
        if href and rel == "alternate":
            best = href.strip()
            break
        if href and not best:
            best = href.strip()
    if not best:
        guid = _child(el, "guid")
        if guid is not None and guid.get("isPermaLink", "true") != "false":
            best = (guid.text or "").strip()
    best = urljoin(base, best) if best else ""
    return best if best.startswith(("http://", "https://")) else ""


def _when(el) -> float | None:
    raw = _child_text(el, "pubDate", "published", "updated", "date").strip()
    if not raw:
        return None
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        try:
            when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    # "-0000" and zone-less ISO dates come back naive: they mean UTC, not
    # the hub's local time.
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.timestamp()


def parse(data: bytes, base: str = "", limit: int = MAX_ITEMS) -> dict:
    """``{title, link, items: [{title, link, published, summary}]}``."""
    head = data[:4096].lower()
    if b"<!entity" in head or b"<!entity" in data.lower():
        raise FeedError("The feed declares XML entities, which are not accepted.")
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise FeedError("That address did not return an RSS or Atom feed.") from None
    kind = _local(root.tag)
    if kind == "rss":
        channel = _child(root, "channel")
        if channel is None:
            raise FeedError("The RSS feed has no channel.")
        entries, meta = [c for c in channel if _local(c.tag) == "item"], channel
    elif kind == "feed":
        entries, meta = [c for c in root if _local(c.tag) == "entry"], root
    elif kind == "RDF":
        meta = _child(root, "channel")
        meta = root if meta is None else meta
        entries = [c for c in root if _local(c.tag) == "item"]
    else:
        raise FeedError("That address did not return an RSS or Atom feed.")
    items = []
    for e in entries[: max(1, min(int(limit), MAX_ITEMS))]:
        items.append({"title": _text(_child_text(e, "title"), 200) or "(untitled)",
                      "link": _link(e, base),
                      "published": _when(e),
                      "summary": _text(_child_text(e, "description", "summary", "content"), 280)})
    return {"title": _text(_child_text(meta, "title"), 120), "link": _link(meta, base), "items": items}


class FeedCache:
    """Recent fetches by URL, so a page full of feeds and every reload of it
    costs the remote sites one request per ``ttl``. Failures are kept for a
    minute, so a broken feed is not retried on every refresh."""

    FAIL_TTL = 60.0

    def __init__(self, size: int = 200, concurrency: int = 4) -> None:
        self.size = size
        self._rows: dict[str, tuple[float, dict | None, str | None]] = {}
        self._gate = asyncio.Semaphore(concurrency)

    async def get(self, url: str, *, ttl: float, limit: int, **fetch_kw) -> dict:
        now = time.time()
        hit = self._rows.get(url)
        if hit is not None:
            ts, feed, error = hit
            if now - ts < (self.FAIL_TTL if error else ttl):
                if error:
                    raise FeedError(error)
                return dict(feed, items=feed["items"][:limit], cached=True)
        async with self._gate:
            try:
                feed = parse(await fetch(url, **fetch_kw), base=url)
                feed["fetched"] = time.time()
            except FeedError as e:
                self._put(url, (time.time(), None, str(e)))
                raise
        self._put(url, (time.time(), feed, None))
        return dict(feed, items=feed["items"][:limit], cached=False)

    def _put(self, url: str, row) -> None:
        self._rows.pop(url, None)
        self._rows[url] = row
        while len(self._rows) > self.size:
            self._rows.pop(next(iter(self._rows)))
