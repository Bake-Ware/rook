"""The show page: one small standalone HTML document per show.

Everything here is pure (no I/O), so it is easy to test. The page is served
by the dashboard at ``/show/<id>`` to any browser that has the link (a phone,
a TV, a desktop), without a sign-in, so it is built to be safe to open from
an agent-written subject:

* every piece of text is HTML-escaped; Markdown is rendered by the small,
  escaping renderer below (no raw HTML passes through);
* an SVG diagram is shown as an ``<img>`` of a ``data:`` URI, where scripts in
  it never run;
* :func:`csp` sandboxes the document (an opaque origin: no cookies, no
  same-origin calls to the hub, whatever a page might contain), allows
  scripts only with the page's nonce (Mermaid, from a pinned CDN URL), and
  :data:`HEADERS` keep it out of caches, search engines and ``Referer``.
"""
from __future__ import annotations

import base64
import html
import re
import time

#: Mermaid, pinned (renders a ``diagram`` subject in the viewer's browser).
MERMAID_SRC = "https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.min.js"

HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",
    "X-Content-Type-Options": "nosniff",
    "Cross-Origin-Opener-Policy": "same-origin",
}


def csp(nonce: str) -> str:
    return ("default-src 'none'; img-src https: data:; style-src 'unsafe-inline'; "
            f"script-src 'nonce-{nonce}' https://cdn.jsdelivr.net; font-src data:; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'; "
            "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox")


def esc(text: object) -> str:
    return html.escape(str(text or ""), quote=True)


_SAFE_URL = re.compile(r"^(https?://|mailto:)", re.I)


def _inline(text: str) -> str:
    """One line of Markdown inline syntax, escaped: `code`, **bold**,
    *italic*, [text](http(s) url), bare http(s) links."""
    out: list[str] = []
    pos = 0
    token = re.compile(r"`([^`]+)`|\*\*([^*]+)\*\*|\*([^*\s][^*]*)\*|\[([^\]]+)\]\(([^)\s]+)\)"
                       r"|(https?://[^\s<>()]+)")
    for m in token.finditer(text):
        out.append(esc(text[pos:m.start()]))
        code, bold, ital, label, href, bare = m.groups()
        if code is not None:
            out.append(f"<code>{esc(code)}</code>")
        elif bold is not None:
            out.append(f"<strong>{_inline(bold)}</strong>")
        elif ital is not None:
            out.append(f"<em>{_inline(ital)}</em>")
        elif label is not None:
            if _SAFE_URL.match(href):
                out.append(f'<a href="{esc(href)}" target="_blank" rel="noopener noreferrer">'
                           f"{_inline(label)}</a>")
            else:
                out.append(esc(m.group(0)))
        else:
            out.append(f'<a href="{esc(bare)}" target="_blank" rel="noopener noreferrer">'
                       f"{esc(bare)}</a>")
        pos = m.end()
    out.append(esc(text[pos:]))
    return "".join(out)


_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_NUMBER = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_IMAGE = re.compile(r"^!\[([^\]]*)\]\((https://[^)\s]+)\)\s*$")
_RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")


def _cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def markdown_html(text: str) -> str:
    """A safe subset of Markdown as HTML: headings, paragraphs, bullet and
    numbered lists, fenced code, block quotes, rules, simple tables, https
    images on their own line, and the inline forms of :func:`_inline`.
    Raw HTML is shown as text."""
    lines = str(text or "").replace("\r\n", "\n").split("\n")
    out: list[str] = []
    para: list[str] = []
    i = 0

    def flush() -> None:
        if para:
            out.append("<p>" + "<br>".join(_inline(x) for x in para) + "</p>")
            para.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            flush()
            lang = stripped[3:].strip()
            body = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            i += 1
            if lang.lower() == "mermaid":
                out.append(f'<pre class="mermaid">{esc(chr(10).join(body))}</pre>')
            else:
                out.append(f"<pre><code>{esc(chr(10).join(body))}</code></pre>")
            continue
        if not stripped:
            flush()
            i += 1
            continue
        m = _HEADING.match(stripped)
        if m:
            flush()
            n = len(m.group(1))
            out.append(f"<h{n}>{_inline(m.group(2))}</h{n}>")
            i += 1
            continue
        if _RULE.match(stripped):
            flush()
            out.append("<hr>")
            i += 1
            continue
        m = _IMAGE.match(stripped)
        if m:
            flush()
            out.append(f'<p><img src="{esc(m.group(2))}" alt="{esc(m.group(1))}"></p>')
            i += 1
            continue
        if "|" in stripped and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
            flush()
            head = _cells(stripped)
            rows = []
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_cells(lines[i]))
                i += 1
            out.append("<table><thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in head)
                       + "</tr></thead><tbody>" + "".join(
                           "<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>"
                           for r in rows) + "</tbody></table>")
            continue
        if stripped.startswith(">"):
            flush()
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip()[1:].lstrip())
                i += 1
            out.append("<blockquote>" + markdown_html("\n".join(quote)) + "</blockquote>")
            continue
        for pattern, tag in ((_BULLET, "ul"), (_NUMBER, "ol")):
            if pattern.match(line):
                flush()
                items = []
                while i < len(lines) and pattern.match(lines[i]):
                    items.append(pattern.match(lines[i]).group(1))
                    i += 1
                out.append(f"<{tag}>" + "".join(f"<li>{_inline(x)}</li>" for x in items)
                           + f"</{tag}>")
                break
        else:
            para.append(stripped)
            i += 1
    flush()
    return "\n".join(out)


def svg_img(svg: str, alt: str = "") -> str:
    data = base64.b64encode(str(svg).encode("utf-8")).decode("ascii")
    return f'<img class="subject" src="data:image/svg+xml;base64,{data}" alt="{esc(alt)}">'


def body_html(rec: dict) -> tuple[str, bool]:
    """``(html, needs_mermaid)`` for a show record's subject."""
    kind, body = rec.get("kind"), str(rec.get("body") or "")
    title = rec.get("title") or ""
    if kind == "image":
        return f'<img class="subject" src="{esc(body)}" alt="{esc(title)}">', False
    if kind == "svg":
        return svg_img(body, title), False
    if kind == "mermaid":
        return f'<pre class="mermaid">{esc(body)}</pre>', True
    if kind in ("markdown", "knowledge"):
        out = markdown_html(body)
        return out, 'class="mermaid"' in out
    return "<p>" + "<br>".join(esc(x) for x in body.split("\n")) + "</p>", False


STYLE = """
:root{--bg:#fbfaf7;--fg:#1d1c1a;--muted:#6b675f;--line:#e2ded6;--code:#f1eee8;--link:#2457a6}
@media (prefers-color-scheme:dark){:root{--bg:#141412;--fg:#ecebe7;--muted:#a19d94;
--line:#34322e;--code:#22211e;--link:#8db4f0}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:18px/1.55 system-ui,-apple-system,
"Segoe UI",Roboto,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 40px}
h1.title{font-size:1.6em;line-height:1.25;margin:0 0 16px}
img.subject,main img{max-width:100%;height:auto;display:block;margin:0 auto}
pre{background:var(--code);padding:12px;border-radius:6px;overflow:auto;font-size:.85em}
pre.mermaid{background:transparent;text-align:center}
code{background:var(--code);padding:0 .25em;border-radius:3px}
pre code{background:none;padding:0}
a{color:var(--link)}
table{border-collapse:collapse;width:100%;margin:12px 0;font-size:.95em}
th,td{border:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
blockquote{margin:12px 0;padding:0 14px;border-left:3px solid var(--line);color:var(--muted)}
footer{margin-top:28px;color:var(--muted);font-size:.8em}
@media (min-width:1600px){body{font-size:24px}main{max-width:1500px}}
"""


def page_html(rec: dict, nonce: str, now: float | None = None) -> str:
    """The whole document for one show record."""
    now = time.time() if now is None else now
    inner, mermaid = body_html(rec)
    title = rec.get("title") or "Rook"
    left = max(0, int(float(rec.get("expires") or now) - now))
    mins = max(1, round(left / 60))
    when = f"{mins} minute{'s' if mins != 1 else ''}" if mins < 120 else f"{round(mins / 60)} hours"
    script = ""
    if mermaid:
        script = (f'<script nonce="{nonce}" src="{MERMAID_SRC}"></script>'
                  f'<script nonce="{nonce}">try{{mermaid.initialize({{startOnLoad:true,'
                  "securityLevel:'strict',theme:(window.matchMedia&&matchMedia("
                  "'(prefers-color-scheme: dark)').matches)?'dark':'default'}})}}catch(e){{}}"
                  "</script>")
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<meta name=\"robots\" content=\"noindex,nofollow\">"
            f"<title>{esc(title)}</title><style>{STYLE}</style></head><body><main>"
            f"<h1 class=\"title\">{esc(title)}</h1>{inner}"
            f"<footer>Shown by Rook. This page goes away in {when}.</footer>"
            f"</main>{script}</body></html>")


def gone_html() -> str:
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<meta name=\"robots\" content=\"noindex,nofollow\"><title>Gone</title>"
            f"<style>{STYLE}</style></head><body><main><h1 class=\"title\">This page is gone"
            "</h1><p>What Rook showed here has expired or was closed. Ask again to see it."
            "</p></main></body></html>")
