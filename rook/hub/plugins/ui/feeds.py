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
  A host that looks numeric but is not a canonical IP address (``2130706433``,
  ``127.1``, ``0177.0.0.1``, ``0x7f.1``) is refused: some aiohttp versions
  take such hosts as literals and skip the resolver. Link-local and cloud
  metadata addresses are refused even when private addresses are allowed,
  and an IPv4 address embedded in IPv6 (mapped, compatible, NAT64, 6to4) is
  checked as that IPv4 address.
* Size: at most ``max_bytes`` of body, counted as it arrives. The hub asks
  for an unencoded body and inflates a gzip/deflate one itself, counting the
  inflated bytes, so a compressed bomb stops at the limit.
* Time: one deadline for the whole fetch, redirects included.

Parsing is stdlib ElementTree on the size-capped bytes, in a worker thread;
an expat pre-pass refuses any document with a DOCTYPE (so entity
declarations, in any encoding, never reach the parser).
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import re
import socket
import time
import xml.etree.ElementTree as ET
import xml.parsers.expat
import zlib
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit


MAX_REDIRECTS = 3
MAX_URL = 2000
MAX_ITEMS = 30
USER_AGENT = "Rook start page (feed reader)"
ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.9, */*;q=0.5"


class FeedError(Exception):
    """A plain-language reason the feed could not be shown."""


#: Refused even with private addresses allowed: cloud metadata services.
#: (169.254.169.254 is link-local, which is refused as a class.)
METADATA = (ipaddress.ip_network("fd00:ec2::254/128"),)
NAT64 = ipaddress.ip_network("64:ff9b::/96")
COMPAT = ipaddress.ip_network("::/96")


def _embedded_v4(addr):
    """The IPv4 address an IPv6 address carries (mapped, NAT64, 6to4 or the
    old IPv4-compatible form), else the address itself."""
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return addr.ipv4_mapped
        if addr in NAT64 or (addr in COMPAT and int(addr) > 1):
            return ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
        if addr.sixtofour is not None:
            return addr.sixtofour
    return addr


def address_ok(ip: str, allow_private: bool) -> bool:
    """Whether the hub may connect to ``ip`` for a feed."""
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    addr = _embedded_v4(addr)
    if addr.is_unspecified or addr.is_multicast or addr.is_link_local:
        return False
    if any(addr in net for net in METADATA if addr.version == net.version):
        return False
    if allow_private:
        return True
    return addr.is_global


# Hosts made only of numbers (decimal, octal, hex) and dots: inet_aton and
# some aiohttp versions read these as IPv4 addresses ("2130706433" and
# "127.1" are 127.0.0.1).
_NUMERIC_HOST = re.compile(r"(0x[0-9a-f]*|\d+)(\.(0x[0-9a-f]*|\d+))*\.?", re.I)


def numeric_but_not_ip(host: str) -> bool:
    """A host that looks like an address but is not a canonical one."""
    h = host.strip("[]")
    return (":" in h or bool(_NUMERIC_HOST.fullmatch(h))) and not _is_ip(h)


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


def guarded_resolver(allow_private: bool):
    """An aiohttp resolver that resolves a name and keeps only the addresses
    :func:`address_ok` allows; aiohttp then connects to those. (aiohttp
    skips the resolver for IP literals, so :func:`fetch` checks those
    itself.) Built on demand: aiohttp is imported only when a feed is
    fetched, so the plugin loads without it."""
    from aiohttp.abc import AbstractResolver

    class GuardedResolver(AbstractResolver):
        async def resolve(self, host: str, port: int = 0, family: int = socket.AF_UNSPEC):
            loop = asyncio.get_running_loop()
            try:
                infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM, family=family)
            except socket.gaierror:
                raise OSError(f"cannot resolve {host}") from None
            out = [{"hostname": host, "host": sockaddr[0], "port": port, "family": fam,
                    "proto": proto, "flags": socket.AI_NUMERICHOST}
                   for fam, _type, proto, _canon, sockaddr in infos
                   if address_ok(sockaddr[0], allow_private)]
            if not out:
                raise BlockedAddress(host)
            return out

        async def close(self) -> None:
            return None

    return GuardedResolver()


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
    import aiohttp
    blocked = FeedError("That address is on a private or local network, which feeds may not "
                        "reach. The operator can allow it in Settings (Dashboard > Feeds).")
    connector = aiohttp.TCPConnector(resolver=guarded_resolver(allow_private), use_dns_cache=False,
                                     limit=2)
    deadline = aiohttp.ClientTimeout(total=timeout)
    headers = {"User-Agent": USER_AGENT, "Accept": ACCEPT, "Accept-Encoding": "identity"}
    try:
        # auto_decompress off: a gzip body is inflated below, against the cap.
        async with aiohttp.ClientSession(connector=connector, timeout=deadline, trust_env=False,
                                         auto_decompress=False, headers=headers) as http:
            async with asyncio.timeout(timeout):
                for _hop in range(MAX_REDIRECTS + 1):
                    host, _port = check_url(url)
                    if numeric_but_not_ip(host):
                        raise blocked
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
                        body = _Body(resp.headers.get("Content-Encoding", ""), max_bytes)
                        async for chunk in resp.content.iter_chunked(16384):
                            body.add(chunk)
                        return body.done()
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


class _Body:
    """A response body under the size cap. A gzip or deflate body (sent
    although the hub asked for none) is inflated here, ``max_length`` at a
    time, so the cap counts what it inflates to; other encodings are refused."""

    def __init__(self, encoding: str, max_bytes: int) -> None:
        enc = (encoding or "identity").strip().lower()
        if enc in ("gzip", "x-gzip", "deflate"):
            self.inflate = zlib.decompressobj(wbits=47)   # gzip or zlib, by header
        elif enc == "identity":
            self.inflate = None
        else:
            raise FeedError(f"The feed was sent with an encoding the hub does not read ({enc}).")
        self.max = max_bytes
        self.raw = 0
        self.out = bytearray()

    def _room(self) -> None:
        if len(self.out) > self.max:
            raise FeedError(_too_big(self.max))

    def add(self, chunk: bytes) -> None:
        self.raw += len(chunk)
        if self.raw > self.max:
            raise FeedError(_too_big(self.max))
        if self.inflate is None:
            self.out += chunk
            return self._room()
        data = chunk
        try:
            while data:
                self.out += self.inflate.decompress(data, self.max - len(self.out) + 1)
                self._room()
                data = self.inflate.unconsumed_tail
        except zlib.error:
            raise FeedError("The feed's compressed body is damaged.") from None

    def done(self) -> bytes:
        if self.inflate is not None:
            try:
                self.out += self.inflate.flush(self.max - len(self.out) + 1)
            except zlib.error:
                raise FeedError("The feed's compressed body is damaged.") from None
            self._room()
        return bytes(self.out)


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


class _Doctype(Exception):
    pass


def _refuse(*_args):
    raise _Doctype()


def _no_doctype(data: bytes) -> None:
    """Refuse a document with a DOCTYPE or an entity declaration, whatever
    its encoding (expat reads the encoding declaration and BOM itself)."""
    pre = xml.parsers.expat.ParserCreate()
    pre.StartDoctypeDeclHandler = _refuse
    pre.EntityDeclHandler = _refuse
    try:
        pre.Parse(data, True)
    except _Doctype:
        raise FeedError("The feed declares a DOCTYPE or XML entities, which are not accepted.") from None
    except xml.parsers.expat.ExpatError:
        raise FeedError("That address did not return an RSS or Atom feed.") from None


def parse(data: bytes, base: str = "", limit: int = MAX_ITEMS) -> dict:
    """``{title, link, items: [{title, link, published, summary}]}``. Blocking:
    call it in a thread."""
    _no_doctype(data)
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
    """Recent fetches by URL (and by whether private addresses were allowed,
    so turning that setting off is not undone by the cache), so a page full
    of feeds and every reload of it costs the remote sites one request per
    ``ttl``. Failures are kept for a minute, so a broken feed is not retried
    on every refresh. At most ``concurrency`` fetches run at once, and at
    most ``per_account`` for one account."""

    FAIL_TTL = 60.0

    def __init__(self, size: int = 200, concurrency: int = 4, per_account: int = 2) -> None:
        self.size = size
        self.per_account = per_account
        self._rows: dict[tuple, tuple[float, dict | None, str | None]] = {}
        self._gate = asyncio.Semaphore(concurrency)
        self._busy: dict[str, int] = {}

    async def get(self, url: str, *, ttl: float, limit: int, account: str = "",
                  allow_private: bool = False, **fetch_kw) -> dict:
        now = time.time()
        key = (url, bool(allow_private))
        hit = self._rows.get(key)
        if hit is not None:
            ts, feed, error = hit
            if now - ts < (self.FAIL_TTL if error else ttl):
                if error:
                    raise FeedError(error)
                return dict(feed, items=feed["items"][:limit], cached=True)
        if self._busy.get(account, 0) >= self.per_account:
            raise FeedError("Your other feeds are still loading; this one will load on the next refresh.")
        self._busy[account] = self._busy.get(account, 0) + 1
        try:
            async with self._gate:
                try:
                    data = await fetch(url, allow_private=allow_private, **fetch_kw)
                    feed = await asyncio.to_thread(parse, data, url)
                    feed["fetched"] = time.time()
                except FeedError as e:
                    self._put(key, (time.time(), None, str(e)))
                    raise
        finally:
            left = self._busy.get(account, 1) - 1
            if left > 0:
                self._busy[account] = left
            else:
                self._busy.pop(account, None)
        self._put(key, (time.time(), feed, None))
        return dict(feed, items=feed["items"][:limit], cached=False)

    def _put(self, key, row) -> None:
        self._rows.pop(key, None)
        self._rows[key] = row
        while len(self._rows) > self.size:
            self._rows.pop(next(iter(self._rows)))
