"""A small client for an OpenAI-compatible chat endpoint (``/v1/models``,
``/v1/chat/completions``), used by the home agent.

The transport is one injectable coroutine, ``http(method, url, body, headers,
timeout) -> (status, data)``, so tests swap in a fake server and the hub never
needs an SDK. Every call has a total timeout; errors are raised as
:class:`LLMError` with the API key scrubbed from the message.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

HttpFn = Callable[[str, str, "dict | None", dict, float], Awaitable["tuple[int, Any]"]]

PROVIDERS = ("openai",)
_THINK = re.compile(r"<think>.*?</think>\s*", re.S | re.I)


class LLMError(RuntimeError):
    """The model endpoint could not answer (unreachable, refused, malformed)."""


async def aiohttp_request(method: str, url: str, body: dict | None, headers: dict,
                          timeout: float) -> tuple[int, Any]:
    import aiohttp
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as http:
        async with http.request(method, url, json=body, headers=headers) as r:
            text = await r.text()
            try:
                data = json.loads(text) if text else None
            except ValueError:
                data = text
            return r.status, data


def base_url(url: str) -> str:
    """Normalise a configured base URL: no trailing slash, and a pasted
    ``.../chat/completions`` or ``.../models`` is cut back to the base."""
    url = (url or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/completions", "/models"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    return url


def clean_reply(text: str | None) -> str:
    """Drop ``<think>`` blocks some local models leave in the content."""
    return _THINK.sub("", text or "").strip()


@dataclass
class ChatClient:
    url: str
    model: str = ""
    api_key: str | None = None
    timeout: float = 60.0
    http: HttpFn = field(default=aiohttp_request)

    def __post_init__(self) -> None:
        self.url = base_url(self.url)
        u = urlparse(self.url)
        if u.scheme not in ("http", "https") or not u.netloc:
            raise LLMError("the base URL must be http(s)://host[:port]/v1")

    @property
    def host(self) -> str:
        return urlparse(self.url).netloc

    def _scrub(self, text: str) -> str:
        if self.api_key and len(self.api_key) >= 4:
            text = text.replace(self.api_key, "***")
        return text

    async def _call(self, method: str, path: str, body: dict | None = None,
                    timeout: float | None = None) -> Any:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        wait = float(timeout or self.timeout)
        try:
            status, data = await asyncio.wait_for(
                self.http(method, self.url + path, body, headers, wait), wait + 1.0)
        except (asyncio.TimeoutError, TimeoutError):
            raise LLMError(f"{self.host} did not answer within {wait:g}s") from None
        except LLMError:
            raise
        except Exception as e:  # noqa: BLE001 - transport failures become one error type
            raise LLMError(self._scrub(f"cannot reach {self.host}: {type(e).__name__}: {e}"[:300])) from None
        if status >= 400:
            msg = ""
            if isinstance(data, dict):
                err = data.get("error")
                msg = err.get("message", "") if isinstance(err, dict) else str(err or data.get("message") or "")
            elif isinstance(data, str):
                msg = data
            raise LLMError(self._scrub(f"{path.lstrip('/')}: HTTP {status} {msg}".strip()[:300]))
        return data

    async def models(self, timeout: float | None = None) -> list[str]:
        """Model ids from ``GET /models`` (sorted, de-duplicated)."""
        data = await self._call("GET", "/models", timeout=timeout)
        rows = data.get("data") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise LLMError("models: the reply has no data list")
        ids = {str(r.get("id")) for r in rows if isinstance(r, dict) and r.get("id")}
        return sorted(ids)

    async def chat(self, messages: list[dict], *, max_tokens: int | None = None,
                   temperature: float | None = None, tools: list[dict] | None = None,
                   timeout: float | None = None) -> dict:
        """One ``POST /chat/completions`` (not streamed). Returns
        ``{content, tool_calls, model, usage, latency_ms, finish_reason}``."""
        if not self.model:
            raise LLMError("no model is set")
        body: dict = {"model": self.model, "messages": messages, "stream": False}
        if max_tokens:
            body["max_tokens"] = int(max_tokens)
        if temperature is not None:
            body["temperature"] = float(temperature)
        if tools:
            body["tools"] = tools
        t0 = time.monotonic()
        data = await self._call("POST", "/chat/completions", body, timeout)
        latency = round((time.monotonic() - t0) * 1000)
        try:
            choice = data["choices"][0]
            msg = choice.get("message") or {}
        except (KeyError, IndexError, TypeError):
            raise LLMError("chat: the reply has no choices") from None
        return {"content": clean_reply(msg.get("content")),
                "tool_calls": list(msg.get("tool_calls") or []),
                "model": str(data.get("model") or self.model) if isinstance(data, dict) else self.model,
                "usage": data.get("usage") if isinstance(data, dict) else None,
                "finish_reason": choice.get("finish_reason"), "latency_ms": latency}
