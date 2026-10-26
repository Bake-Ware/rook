"""Talk to a clean-room hub over its public HTTP surface, stdlib only.

Two identities, like a real install:
- the operator (a person at the dashboard): Basic auth with the dashboard
  password, or the account cookie for /account/* pages;
- an agent: the hub's MCP endpoint with a bearer token.
"""

from __future__ import annotations

import base64
import http.client
import json
import re
import time
import urllib.parse


class HubError(RuntimeError):
    pass


class Hub:
    def __init__(self, base_url: str, password: str, mcp_token: str = "", *, user="operator"):
        self.base = base_url.rstrip("/")
        u = urllib.parse.urlparse(self.base)
        self.host, self.port = u.hostname, u.port or 80
        self.password = password
        self.user = user
        self.mcp_token = mcp_token
        self._cookie = ""
        self._csrf = ""
        self._mcp_session = ""
        self._rpc_id = 0

    # -- raw HTTP ------------------------------------------------------------
    def _req(self, method, path, *, body=None, headers=None, timeout=60):
        c = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            c.request(method, path, body=body, headers=headers or {})
            r = c.getresponse()
            data = r.read()
            return r.status, dict((k.lower(), v) for k, v in r.getheaders()), data
        finally:
            c.close()

    def _basic(self):
        tok = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        return {"Authorization": f"Basic {tok}"}

    def get_json(self, path, *, timeout=60):
        st, _, data = self._req("GET", path, headers=self._basic(), timeout=timeout)
        if st >= 400:
            raise HubError(f"GET {path}: {st} {data[:300]!r}")
        return json.loads(data or b"null")

    def post_json(self, path, obj, *, timeout=120):
        h = self._basic() | {"Content-Type": "application/json"}
        st, _, data = self._req("POST", path, body=json.dumps(obj), headers=h, timeout=timeout)
        if st >= 400:
            raise HubError(f"POST {path}: {st} {data[:500]!r}")
        return json.loads(data or b"null")

    def health(self) -> bool:
        try:
            st, _, _ = self._req("GET", "/health", timeout=5)
            return st == 200
        except OSError:
            return False

    # -- operator (dashboard) --------------------------------------------------
    def workers(self) -> list[dict]:
        rows = self.get_json("/api/band/workers")
        return rows.get("workers", rows) if isinstance(rows, dict) else rows

    def worker(self, name: str) -> dict | None:
        for w in self.workers():
            if w.get("name") == name:
                return w
        return None

    def wait_worker(self, name: str, *, timeout=300, online=True) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                w = self.worker(name)
            except (HubError, OSError):
                w = None
            fresh = w is not None and (w.get("last_seen_age_secs") or 0) <= 65
            if online and fresh:
                return w
            if not online and not fresh:
                return w or {}
            time.sleep(3)
        raise HubError(f"worker {name} {'never came online' if online else 'never went offline'}")

    def band_call(self, cap, args=None, *, worker=None, timeout=120):
        """Run a cap as the operator (the dashboard's own call path; never held)."""
        body = {"cap": cap, "args": args or {}, "timeout": max(1, timeout - 5)}
        if worker:
            body["target"] = worker
        return self.post_json("/api/band/call", body, timeout=timeout)

    def hub_cap(self, cap, args=None, *, timeout=120):
        return self.band_call(cap, args, worker="rook", timeout=timeout)

    def approvals(self) -> list[dict]:
        j = self.get_json("/api/approvals")
        if isinstance(j, dict):
            return j.get("pending") or j.get("items") or j.get("approvals") or []
        return j

    def decide(self, hold_id: str, decision="approve", scope="call"):
        return self.post_json(f"/api/approvals/{hold_id}", {"decision": decision, "scope": scope})

    def trust(self, op: str, data: dict, words: str):
        return self.post_json("/api/trust", {"op": op, "data": data, "words": words})

    # account pages (cookie + csrf)
    def login(self):
        st, h, body = self._req("GET", "/account/login")
        m = re.search(r"rook_login_form=([^;]+)", h.get("set-cookie", ""))
        t = re.search(r'name="form_token" value="([^"]+)"', body.decode())
        if not (m and t):
            raise HubError(f"login form not found ({st})")
        data = urllib.parse.urlencode({"form_token": t.group(1), "username": self.user, "password": self.password})
        st, h, _ = self._req("POST", "/account/login", body=data, headers={
            "Content-Type": "application/x-www-form-urlencoded", "Cookie": f"rook_login_form={m.group(1)}"})
        c = re.search(r"rook_account=([^;]+)", h.get("set-cookie", ""))
        if not c:
            raise HubError(f"login failed ({st})")
        self._cookie = c.group(1)
        sess = self.account_get("/account/session")
        self._csrf = (sess.get("user") or {}).get("csrf", "")
        return sess

    def account_get(self, path):
        st, _, data = self._req("GET", path, headers={"Cookie": f"rook_account={self._cookie}"})
        if st >= 400:
            raise HubError(f"GET {path}: {st} {data[:300]!r}")
        return json.loads(data or b"null")

    def account_post(self, path, obj, *, timeout=120):
        obj = {"csrf": self._csrf} | obj
        st, _, data = self._req("POST", path, body=json.dumps(obj), timeout=timeout, headers={
            "Cookie": f"rook_account={self._cookie}", "Content-Type": "application/json"})
        if st >= 400:
            raise HubError(f"POST {path}: {st} {data[:300]!r}")
        return json.loads(data or b"null")

    def pairing_code(self) -> dict:
        """What 'Get a pairing code' on the Install page does."""
        if not self._cookie:
            self.login()
        sess = self.account_get("/account/session")
        band = next((b["id"] for b in sess.get("bands", []) if b.get("active")), None) or sess["bands"][0]["id"]
        boundary = "cleanroom" + str(int(time.time()))
        parts = []
        for k, v in {"csrf": self._csrf, "op": "pair", "band_id": band}.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n')
        body = "".join(parts) + f"--{boundary}--\r\n"
        st, _, data = self._req("POST", "/account/action", body=body, headers={
            "Cookie": f"rook_account={self._cookie}", "X-Rook-View": "account",
            "Content-Type": f"multipart/form-data; boundary={boundary}"})
        if st >= 400:
            raise HubError(f"pairing code: {st} {data[:300]!r}")
        j = json.loads(data)
        if j.get("csrf"):
            self._csrf = j["csrf"]
        return j["pairing"]

    def settings_view(self, view: str):
        if not self._cookie:
            self.login()
        return self.account_get(f"/account/settings/api?view={urllib.parse.quote(view)}")

    # -- agent (MCP) ---------------------------------------------------------------
    def _mcp_post(self, payload, *, timeout=300):
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
             "Authorization": f"Bearer {self.mcp_token}"}
        if self._mcp_session:
            h["Mcp-Session-Id"] = self._mcp_session
        st, hdrs, data = self._req("POST", "/mcp", body=json.dumps(payload), headers=h, timeout=timeout)
        if hdrs.get("mcp-session-id"):
            self._mcp_session = hdrs["mcp-session-id"]
        if st >= 400:
            raise HubError(f"MCP {payload.get('method')}: {st} {data[:300]!r}")
        text = data.decode()
        if "text/event-stream" in hdrs.get("content-type", ""):
            msgs = [json.loads(line[5:]) for line in text.splitlines() if line.startswith("data:") and line[5:].strip()]
            for msg in msgs:
                if msg.get("id") == payload.get("id"):
                    return msg
            return msgs[-1] if msgs else {}
        return json.loads(text) if text.strip() else {}

    def _mcp_init(self):
        self._rpc_id += 1
        self._mcp_post({"jsonrpc": "2.0", "id": self._rpc_id, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "rook-cleanroom", "version": "1"}}})
        self._mcp_post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def mcp(self, tool: str, args: dict | None = None, *, timeout=300):
        if not self._mcp_session:
            self._mcp_init()
        self._rpc_id += 1
        msg = self._mcp_post({"jsonrpc": "2.0", "id": self._rpc_id, "method": "tools/call",
                              "params": {"name": tool, "arguments": args or {}}}, timeout=timeout)
        if "error" in msg:
            raise HubError(f"MCP {tool}: {msg['error']}")
        res = msg.get("result") or {}
        if res.get("structuredContent") is not None:
            sc = res["structuredContent"]
            return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
        texts = [c.get("text", "") for c in res.get("content", []) if c.get("type") == "text"]
        joined = "\n".join(texts)
        try:
            return json.loads(joined)
        except ValueError:
            return joined

    def mcp_call(self, cap, args=None, *, worker="rook", timeout=300):
        return self.mcp("rook_call", {"cap": cap, "worker": worker, "why": f"Cleanroom check: {cap}",
                                      "args": args or {}}, timeout=timeout)
