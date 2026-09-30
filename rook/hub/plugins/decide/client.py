"""Talking to a one-pass decision model: question normalisation, adapters and
transports.

A decision model answers a batch of typed questions about one ``state`` in a
single forward pass. Rook speaks two dialects (``adapter``):

``diffucision``
    ``POST /decide {state, questions, temperature?}``; ``GET /healthz``
    (``{"ok": bool}``) and ``GET /info``. Up to 64 questions and 26 options
    per question. ``noul`` answers carry ``noul`` (P(true)) and
    ``probabilities``.
``decision-engine``
    The typed decision engine: the same request, up to 24 questions and 52
    options. ``noul`` answers carry only ``noul`` and ``confidence``;
    ``/healthz`` returns ``{"status", "ready"}``. No ``temperature``.

and reaches it through two transports (the ``endpoint`` connection string):

``http(s)://host:port``
    The base URL; ``/decide``, ``/healthz`` and ``/info`` are appended (a
    trailing ``/decide`` is tolerated, a query string such as
    ``?adapter=web`` is kept on ``/decide`` and ``/info``). An optional
    bearer token.
``cap://<worker>/<cap>``
    A band capability. A worker custom cap under ``cmd.`` (``cmd.decide-run``
    wrapping a CLI) gets ``{"payload": "<request JSON>"}`` and its ``stdout``
    is parsed; any other cap gets the request as its arguments and returns
    the response. Health and info go to the sibling caps whose name has
    ``run`` replaced by ``health`` / ``info`` (``cmd.decide-health``).

Every answer is normalised to one shape (see :func:`normalise_answer`), and a
batch larger than the adapter's limit is split into several passes.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ....core.plugin import Resource

ADAPTERS = ("diffucision", "decision-engine")

#: Per-adapter request limits (from each service's request validation).
LIMITS = {
    "diffucision": {"max_questions": 64, "max_options": 26, "temperature": True},
    "decision-engine": {"max_questions": 24, "max_options": 52, "temperature": False},
}

#: Accepted spellings of the yes/no question type.
_NOUL = {"noul", "yes-no", "yesno", "yes_no", "bool", "boolean"}
TYPES = ("choice", "score", "noul")


class DecisionError(RuntimeError):
    """The decision model could not answer (unreachable, refused, malformed)."""


# -- questions --------------------------------------------------------------

def normalise_questions(questions: Any, max_options: int) -> list[dict]:
    """Validate a batch and return it in the wire shape both services accept:
    ``{id, type, instructions, options|levels}``. Raises ``ValueError`` with a
    message naming the bad question."""
    if not isinstance(questions, list) or not questions:
        raise ValueError("questions must be a non-empty list")
    out: list[dict] = []
    seen: set[str] = set()
    for i, q in enumerate(questions):
        if not isinstance(q, dict):
            raise ValueError(f"question {i + 1} must be an object")
        qid = str(q.get("id") or f"q{i + 1}")
        if qid in seen:
            raise ValueError(f"{qid}: question ids must be unique")
        seen.add(qid)
        kind = str(q.get("type") or "choice").strip().lower()
        if kind in _NOUL:
            kind = "noul"
        if kind not in TYPES:
            raise ValueError(f"{qid}: type must be choice, score or noul (yes-no), got {kind!r}")
        text = str(q.get("question") or q.get("instructions") or q.get("text") or "").strip()
        if not text:
            raise ValueError(f"{qid}: question text is required")
        item: dict[str, Any] = {"id": qid, "type": kind, "instructions": text}
        if kind == "noul":
            crit = q.get("criteria")
            if isinstance(crit, dict) and set(crit) <= {"true", "false"}:
                item["criteria"] = {k: str(v) for k, v in crit.items()}
        else:
            key = "levels" if kind == "score" else "options"
            spec = q.get(key)
            if spec is None:
                spec = q.get("options") if kind == "score" else q.get("criteria")
            if spec is None and kind == "score":
                spec = q.get("criteria")
            if isinstance(spec, dict):
                labels = [str(k) for k in spec]
                spec = {str(k): str(v) for k, v in spec.items()}
            elif isinstance(spec, (list, tuple)):
                labels = [str(v) for v in spec]
                spec = labels
            else:
                raise ValueError(f"{qid}: {kind} needs a list (or label->description map) of {key}")
            if len(labels) < 2:
                raise ValueError(f"{qid}: needs at least 2 {key}")
            if len(labels) > max_options:
                raise ValueError(f"{qid}: at most {max_options} {key} for this model")
            if len(set(labels)) != len(labels) or any(not s.strip() for s in labels):
                raise ValueError(f"{qid}: {key} must be unique and non-empty")
            item[key] = spec
        out.append(item)
    return out


def _labels(q: dict) -> list[str]:
    spec = q.get("levels") if q["type"] == "score" else q.get("options")
    if isinstance(spec, dict):
        return list(spec)
    return [str(s) for s in spec or []]


def _num(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if f == f else default  # NaN guard


def normalise_answer(q: dict, a: dict, adapter: str) -> dict:
    """One answer in the plugin's shape:

    * choice: ``{id, type, choice, probabilities, confidence}``
    * noul:   ``{id, type, p, probabilities: {true, false}, confidence}``
    * score:  ``{id, type, level, score, probabilities, confidence}``

    ``confidence`` is the model's top-two margin (uncalibrated)."""
    kind = q["type"]
    out: dict[str, Any] = {"id": q["id"], "type": kind,
                           "confidence": round(_num(a.get("confidence")), 4)}
    probs = a.get("probabilities") if isinstance(a.get("probabilities"), dict) else None
    if kind == "noul":
        p = a.get("noul", a.get("p"))
        if p is None and probs:
            p = probs.get("true")
        p = min(1.0, max(0.0, _num(p)))
        out["p"] = round(p, 4)
        out["probabilities"] = {"true": round(p, 4), "false": round(1 - p, 4)}
        return out
    labels = _labels(q)
    probs = {str(k): round(_num(v), 4) for k, v in (probs or {}).items()}
    out["probabilities"] = probs
    if kind == "choice":
        choice = a.get("choice")
        if choice is None and probs:
            choice = max(probs, key=probs.get)
        out["choice"] = None if choice is None else str(choice)
        return out
    level = a.get("level")
    if level is None and probs:
        level = max(probs, key=probs.get)
    # diffucision indexes list levels 0..n-1; map back to the label when it can.
    if isinstance(spec := q.get("levels"), list) and str(level).isdigit() \
            and int(level) < len(spec) and str(level) not in labels:
        level = spec[int(level)]
    out["level"] = None if level is None else str(level)
    out["score"] = round(_num(a.get("score", a.get("expected"))), 4)
    return out


def split(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)] or [[]]


# -- the client -------------------------------------------------------------

HttpFn = Callable[[str, str, "dict | None", dict, float], Awaitable[tuple[int, Any]]]


async def _aiohttp_request(method: str, url: str, body: dict | None, headers: dict,
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


@dataclass
class DecisionClient:
    """One configured endpoint. ``resource`` is the parsed connection string
    (``cap://`` resources carry the host's band caller)."""

    resource: Resource
    adapter: str = "diffucision"
    token: str | None = None
    timeout: float = 10.0
    http: HttpFn = _aiohttp_request

    def __post_init__(self) -> None:
        if self.adapter not in ADAPTERS:
            raise ValueError(f"adapter must be one of {ADAPTERS}, got {self.adapter!r}")

    @property
    def limits(self) -> dict:
        return LIMITS[self.adapter]

    # transport ---------------------------------------------------------
    def _url(self, path: str) -> str:
        """``base + path``, keeping the endpoint's query string (a
        serve-and-train diffucision picks a named adapter with
        ``?adapter=<name>``)."""
        url, _, query = self.resource.url.partition("?")
        url = url.rstrip("/")
        for suffix in ("/decide", "/healthz", "/info"):
            if url.endswith(suffix):
                url = url[: -len(suffix)]
        return url + path + (f"?{query}" if query and path != "/healthz" else "")

    async def _http(self, method: str, path: str, body: dict | None = None) -> Any:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            status, data = await self.http(method, self._url(path), body, headers,
                                           self.timeout)
        except DecisionError:
            raise
        except Exception as e:  # connection refused, timeout, DNS ...
            raise DecisionError(f"decision model unreachable: {type(e).__name__}: {e}") from None
        if status >= 400:
            detail = data.get("detail", data) if isinstance(data, dict) else data
            raise DecisionError(f"decision model returned HTTP {status}: "
                                f"{json.dumps(detail)[:500] if not isinstance(detail, str) else detail[:500]}")
        return data

    def _sibling(self, what: str) -> str:
        cap = self.resource.path
        head, _, last = cap.rpartition(".")
        if "run" in last:
            return (head + "." if head else "") + last.replace("run", what)
        if last in ("decide", "decide_run"):
            return (head + "." if head else "") + what
        raise DecisionError(f"cannot derive a {what} cap from {cap!r}; name the "
                            "decision cap ...run (e.g. cmd.decide-run)")

    async def _cap(self, cap: str, body: dict | None) -> Any:
        wrapped = cap.startswith("cmd.")
        args = ({"payload": json.dumps(body, separators=(",", ":"))} if body is not None else {}) \
            if wrapped else (body or {})
        res = Resource(f"cap://{self.resource.target}/{cap}", "cap", self.resource.target, cap,
                       self.resource._caller)
        try:
            out = await res.call(args, timeout=self.timeout)
        except Exception as e:
            raise DecisionError(f"decision cap {cap} failed: {type(e).__name__}: {e}") from None
        if wrapped and isinstance(out, dict) and "stdout" in out:
            try:
                parsed = json.loads(out.get("stdout") or "null")
            except ValueError:
                raise DecisionError(f"{cap}: stdout is not JSON") from None
            if not out.get("ok", True) or (isinstance(parsed, dict) and parsed.get("ok") is False):
                err = parsed.get("error") if isinstance(parsed, dict) else out.get("stderr")
                raise DecisionError(f"{cap} failed: {str(err)[:500]}")
            return parsed
        if isinstance(out, dict) and out.get("ok") is False:
            raise DecisionError(f"{cap} failed: {str(out.get('error'))[:500]}")
        return out

    async def _request(self, op: str, body: dict | None = None) -> Any:
        if self.resource.scheme in ("http", "https"):
            path = {"decide": "/decide", "health": "/healthz", "info": "/info"}[op]
            return await self._http("POST" if body is not None else "GET", path, body)
        if self.resource.scheme == "cap":
            cap = self.resource.path if op == "decide" else self._sibling(op)
            return await self._cap(cap, body)
        raise DecisionError(f"endpoint scheme {self.resource.scheme!r} is not supported; "
                            "use http(s):// or cap://")

    # API ---------------------------------------------------------------
    async def decide(self, state: Any, questions: list[dict],
                     temperature: float | None = None) -> dict:
        """Answer ``questions`` (raw, validated here) about ``state``. Returns
        ``{answers, passes, latency_ms, model, calibration}``."""
        qs = normalise_questions(questions, self.limits["max_options"])
        t0 = time.perf_counter()
        answers: list[dict] = []
        model = calibration = None
        passes = 0
        for chunk in split(qs, self.limits["max_questions"]):
            body: dict[str, Any] = {"state": state, "questions": chunk}
            if temperature is not None and self.limits["temperature"]:
                body["temperature"] = float(temperature)
            data = await self._request("decide", body)
            if not isinstance(data, dict) or not isinstance(data.get("answers"), (list, dict)):
                raise DecisionError("decision model response has no answers")
            raw = data["answers"]
            by_id = raw if isinstance(raw, dict) else {
                str(a.get("id")): a for a in raw if isinstance(a, dict)}
            for q in chunk:
                a = by_id.get(q["id"])
                if not isinstance(a, dict):
                    raise DecisionError(f"decision model did not answer {q['id']}")
                answers.append(normalise_answer(q, a, self.adapter))
            passes += int(data.get("passes") or 1)
            model = model or data.get("model")
            calibration = calibration or data.get("calibration")
        return {"answers": answers, "passes": passes,
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                "model": model, "calibration": calibration or "uncalibrated",
                "adapter": self.adapter}

    async def health(self) -> dict:
        t0 = time.perf_counter()
        try:
            data = await self._request("health")
        except DecisionError as e:
            return {"ok": False, "ready": False, "error": str(e), "adapter": self.adapter}
        ready = False
        if isinstance(data, dict):
            ready = bool(data.get("ready", data.get("ok", data.get("status") == "ok")))
        return {"ok": True, "ready": ready, "adapter": self.adapter,
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}

    async def info(self) -> dict:
        data = await self._request("info")
        data = data if isinstance(data, dict) else {"raw": data}
        keep = ("model", "adapter", "lora_path", "temperature", "calibration", "dtype",
                "quant", "max_questions", "max_options", "max_tokens", "calls")
        out = {k: data[k] for k in keep if k in data}
        out["client"] = {"adapter": self.adapter, "transport": self.resource.scheme,
                         **{k: v for k, v in self.limits.items() if k != "temperature"}}
        return out
