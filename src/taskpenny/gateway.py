"""Vercel AI Gateway client: Jev decisions (/v1/evaluate) and chat models (/v1/chat/completions).

One key serves both. Every call returns its token usage and its cost in USD.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from . import __version__
from .catalog import Catalog

DEFAULT_BASE_URL = "https://ai-gateway.vercel.sh"
RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}


class GatewayError(RuntimeError):
    def __init__(self, status: int, message: str, body: Any = None):
        super().__init__(f"{status}: {message}")
        self.status = status
        self.body = body


@dataclass
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0  # USD
    cost_source: str = "catalog"  # "gateway" when the gateway reported it


@dataclass
class EvalResult:
    answers: dict[str, dict]
    confidence: dict[str, float]
    usage: Usage
    latency_ms: int

    def choice(self, name: str) -> tuple[str | None, float | None, dict]:
        a = self.answers.get(name) or {}
        return a.get("choice"), self.confidence.get(name), a.get("probabilities", {})

    def boolean(self, name: str) -> float | None:
        a = self.answers.get(name) or {}
        p = a.get("probability")
        return float(p) if p is not None else None

    def score(self, name: str) -> tuple[float | None, float | None]:
        a = self.answers.get(name) or {}
        s = a.get("score")
        return (float(s) if s is not None else None), self.confidence.get(name)


@dataclass
class ChatResult:
    text: str
    model: str
    usage: Usage
    latency_ms: int
    raw: dict = field(default_factory=dict, repr=False)


class GatewayLike(Protocol):
    async def evaluate(self, state: Any, questions: dict[str, dict]) -> EvalResult: ...
    async def chat(self, model: str, messages: list[dict], *, max_tokens: int | None = None,
                   temperature: float | None = None, json_mode: bool = False,
                   reasoning: str | None = None) -> ChatResult: ...
    async def aclose(self) -> None: ...


def confidence_from_probs(probs: dict[str, float]) -> float | None:
    """Approximate confidence when Jev does not send one: (k * pmax - 1) / (k - 1)."""
    if not probs:
        return None
    k = len(probs)
    if k < 2:
        return 1.0
    pmax = max(probs.values())
    return max(0.0, min(1.0, (k * pmax - 1) / (k - 1)))


def chat_timeout(max_tokens: int | None) -> float:
    """Seconds to wait for a chat answer: a minute and a half, plus time to write the allowed output slowly."""
    return 90.0 + (max_tokens or 4000) * 0.06


def _gateway_cost(data: dict) -> float | None:
    for path in (("providerMetadata", "gateway", "cost"), ("provider_metadata", "gateway", "cost"),
                 ("usage", "cost"), ("usage", "total_cost")):
        cur: Any = data
        for key in path:
            cur = cur.get(key) if isinstance(cur, dict) else None
        if cur not in (None, ""):
            try:
                return float(cur)
            except (TypeError, ValueError):
                pass
    return None


class Gateway:
    """Async client for Vercel AI Gateway."""

    def __init__(self, api_key: str | None = None, *, catalog: Catalog, base_url: str | None = None,
                 timeout: float = 120.0, eval_timeout: float = 8.0, retries: int = 2,
                 client: httpx.AsyncClient | None = None):
        self.api_key = api_key or os.environ.get("AI_GATEWAY_API_KEY", "")
        if not self.api_key:
            raise GatewayError(401, "No AI_GATEWAY_API_KEY. Create one at vercel.com -> AI Gateway -> API Keys, "
                                    "or run with --dry-run to see Taskpenny work without a key.")
        self.catalog = catalog
        self.base_url = (base_url or os.environ.get("AI_GATEWAY_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.timeout, self.eval_timeout, self.retries = timeout, eval_timeout, retries
        self._client = client or httpx.AsyncClient(
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json",
                     "User-Agent": f"taskpenny/{__version__}"},
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post(self, path: str, body: dict, timeout: float, retry_timeouts: bool = True) -> tuple[dict, int]:
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            t0 = time.perf_counter()
            try:
                r = await self._client.post(self.base_url + path, json=body, timeout=timeout)
            except httpx.TimeoutException as e:
                if not retry_timeouts:
                    # the provider may still finish and bill this call: never send it again blindly
                    raise GatewayError(408, f"no answer after {timeout:.0f} s; the provider may still bill "
                                            "this call") from e
                last = e
            except httpx.TransportError as e:
                last = e
            else:
                ms = int((time.perf_counter() - t0) * 1000)
                if r.status_code < 400:
                    return r.json(), ms
                try:
                    payload = r.json()
                except ValueError:
                    payload = r.text
                msg = payload.get("error", payload) if isinstance(payload, dict) else payload
                if isinstance(msg, dict):
                    msg = msg.get("message") or json.dumps(msg)[:300]
                last = GatewayError(r.status_code, str(msg)[:500], payload)
                if r.status_code not in RETRY_STATUS:
                    raise last
                ra = r.headers.get("retry-after")
                if ra:
                    try:
                        await asyncio.sleep(min(float(ra), 10.0))
                        continue
                    except ValueError:
                        pass
            if attempt < self.retries:
                await asyncio.sleep(min(8.0, 0.6 * 2 ** attempt + random.random() * 0.3))
        if isinstance(last, GatewayError):
            raise last
        raise GatewayError(0, f"network error: {last}")

    async def evaluate(self, state: Any, questions: dict[str, dict]) -> EvalResult:
        body = {"model": self.catalog.decider.id, "state": state, "questions": questions}
        data, ms = await self._post("/v1/evaluate", body, self.eval_timeout)
        answers = data.get("answers", {})
        meta = (data.get("providerMetadata") or data.get("provider_metadata") or {}).get("typesafe", {})
        conf = {k: float(v) for k, v in (meta.get("confidence") or {}).items()}
        for name, a in answers.items():
            if name not in conf and a.get("type") in ("choice", "score"):
                c = confidence_from_probs(a.get("probabilities") or {})
                if c is not None:
                    conf[name] = c
        u = data.get("usage") or {}
        tin = int(u.get("inputTokens") or u.get("input_tokens") or 0)
        cost = _gateway_cost(data)
        usage = Usage(tin, 0, cost if cost is not None else self.catalog.decider.cost(tin, 0),
                      "gateway" if cost is not None else "catalog")
        return EvalResult(answers=answers, confidence=conf, usage=usage, latency_ms=ms)

    async def chat(self, model: str, messages: list[dict], *, max_tokens: int | None = None,
                   temperature: float | None = None, json_mode: bool = False,
                   reasoning: str | None = None) -> ChatResult:
        """reasoning: None (the model's default), "off", or an effort level ("low", "medium", "high")."""
        body: dict[str, Any] = {"model": model, "messages": messages}
        if reasoning == "off":
            body["reasoning"] = {"enabled": False}
        elif reasoning:
            body["reasoning"] = {"effort": reasoning}
        if max_tokens:
            body["max_tokens"] = max_tokens
        if temperature is not None:
            body["temperature"] = temperature
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        # long answers from strong models can take minutes: wait in proportion to the output allowed
        timeout = max(self.timeout, chat_timeout(max_tokens))
        try:
            data, ms = await self._post("/v1/chat/completions", body, timeout, retry_timeouts=False)
        except GatewayError as e:
            if e.status in (400, 422) and ("response_format" in body or "reasoning" in body):
                # some providers refuse response_format or reasoning controls: ask again without them
                body.pop("response_format", None)
                body.pop("reasoning", None)
                data, ms = await self._post("/v1/chat/completions", body, timeout, retry_timeouts=False)
            else:
                raise
        choice = (data.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content") or ""
        if isinstance(text, list):  # content parts
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        u = data.get("usage") or {}
        tin = int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
        tout = int(u.get("completion_tokens") or u.get("output_tokens") or 0)
        cost = _gateway_cost(data)
        if cost is None:
            try:
                cost_val, src = self.catalog.get(model).cost(tin, tout), "catalog"
            except KeyError:
                cost_val, src = 0.0, "unknown"
        else:
            cost_val, src = cost, "gateway"
        return ChatResult(text=text, model=data.get("model", model), usage=Usage(tin, tout, cost_val, src),
                          latency_ms=ms, raw=data)
