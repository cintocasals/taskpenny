"""Vercel AI Gateway client: Jev decisions (/v1/evaluate) and chat models (/v1/chat/completions).

One key serves both. Every call returns its token usage and its cost in USD.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from . import __version__
from .catalog import Catalog

DEFAULT_BASE_URL = "https://ai-gateway.vercel.sh"
RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}
# A chat call that timed out at the gateway may still have been billed by the provider: never send it again.
NO_CHAT_RETRY = {504}


KEY_LIKE = re.compile(r"\b(?:sk-|sk_|AIza|vck_|gh[pousr]_)[\w*.-]+")


class GatewayError(RuntimeError):
    def __init__(self, status: int, message: str, body: Any = None):
        # providers sometimes echo part of a wrong key; it must not reach run files, logs or API answers
        message = KEY_LIKE.sub("[key hidden]", str(message))
        super().__init__(f"{status}: {message}")
        self.status = status
        self.body = body


class BudgetExceeded(RuntimeError):
    """The run's budget cannot pay for the next call. Raised before the call is made, never after."""


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
        c = a.get("choice")
        return (None if c is None else str(c)), self.confidence.get(name), a.get("probabilities") or {}

    def boolean(self, name: str) -> float | None:
        """The probability of yes, or None when Jev's answer has none (an unknown verdict, not a no)."""
        return _float01((self.answers.get(name) or {}).get("probability"))

    def score(self, name: str) -> tuple[float | None, float | None]:
        return _finite((self.answers.get(name) or {}).get("score")), self.confidence.get(name)


def _float01(x: Any) -> float | None:
    v = _finite(x)  # NaN or Infinity is unknown
    return None if v is None else max(0.0, min(1.0, v))


@dataclass
class ChatResult:
    text: str
    model: str
    usage: Usage
    latency_ms: int
    raw: dict = field(default_factory=dict, repr=False)
    finish_reason: str = ""  # "length" when the answer was cut at max_tokens


def bad_answer(what: str) -> "GatewayError":
    """An answer Taskpenny cannot read (HTML from a proxy, a missing field, a wrong type) is a gateway error,
    so the engine can fall back or stop cleanly instead of crashing and losing what was already paid."""
    return GatewayError(502, f"{what} sent an answer Taskpenny cannot read")


def read_json(r: httpx.Response, who: str) -> dict:
    try:
        data = r.json()
    except ValueError:
        raise bad_answer(who) from None
    if not isinstance(data, dict):
        raise bad_answer(who)
    return data


def chat_fields(data: dict, who: str) -> tuple[str, dict, str]:
    """Text, usage and finish reason of an OpenAI-style chat answer, checked for shape."""
    try:
        choices = data.get("choices") or [{}]
        choice = choices[0] if isinstance(choices, list) and choices else {}
        if not isinstance(choice, dict):
            raise TypeError
        msg = choice.get("message") or {}
        text = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(text, list):  # content parts
            text = "".join(str(p.get("text", "")) for p in text if isinstance(p, dict))
        usage = data.get("usage") or {}
        if not isinstance(usage, dict):
            raise TypeError
        return str(text or ""), usage, str(choice.get("finish_reason") or "")
    except (TypeError, AttributeError, KeyError, IndexError):
        raise bad_answer(who) from None


def as_int(x: Any) -> int:
    """A token count from a provider: a whole number of zero or more, whatever was sent (NaN, Infinity, text)."""
    try:
        return max(0, int(x or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _finite(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError, OverflowError):
        return None
    return v if math.isfinite(v) else None


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
            v = _finite(cur)
            if v is not None and v >= 0:  # a negative or impossible cost is ignored: the catalog price is used
                return v
    return None


def parse_answers(data: dict) -> tuple[dict[str, dict], dict[str, float]]:
    """Jev's answers and confidences, checked for shape: anything unexpected is a GatewayError, not a crash."""
    answers = data.get("answers")
    if not isinstance(answers, dict) or not all(isinstance(a, dict) for a in answers.values()):
        raise bad_answer("Jev")
    meta = data.get("providerMetadata") or data.get("provider_metadata") or {}
    meta = meta.get("typesafe") if isinstance(meta, dict) else None
    raw_conf = meta.get("confidence") if isinstance(meta, dict) else None
    conf: dict[str, float] = {}
    for k, v in (raw_conf.items() if isinstance(raw_conf, dict) else []):
        c = _float01(v)
        if c is not None:  # a missing confidence is worked out from the probabilities below
            conf[str(k)] = c
    for name, a in answers.items():
        probs = a.get("probabilities")
        clean = {str(k): _float01(v) for k, v in probs.items()} if isinstance(probs, dict) else {}
        a["probabilities"] = {k: v for k, v in clean.items() if v is not None}
        if name not in conf and a.get("type") in ("choice", "score"):
            c = confidence_from_probs(a["probabilities"])
            if c is not None:
                conf[name] = c
    return answers, conf


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
        """retry_timeouts=False (chat calls): a call that may have been billed (a timeout, a gateway 504) is
        never sent again."""
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
                    return read_json(r, "Vercel AI Gateway"), ms
                try:
                    payload = r.json()
                except ValueError:
                    payload = r.text
                msg = payload.get("error", payload) if isinstance(payload, dict) else payload
                if isinstance(msg, dict):
                    msg = msg.get("message") or json.dumps(msg)[:300]
                last = GatewayError(r.status_code, str(msg)[:500], payload)
                if r.status_code not in RETRY_STATUS or (not retry_timeouts and r.status_code in NO_CHAT_RETRY):
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
        answers, conf = parse_answers(data)
        u = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        tin = as_int(u.get("inputTokens") or u.get("input_tokens"))
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
        text, u, finish = chat_fields(data, "Vercel AI Gateway")
        tin = as_int(u.get("prompt_tokens") or u.get("input_tokens"))
        tout = as_int(u.get("completion_tokens") or u.get("output_tokens"))
        cost = _gateway_cost(data)
        if cost is None:
            try:
                cost_val, src = self.catalog.get(model).cost(tin, tout), "catalog"
            except KeyError:
                cost_val, src = 0.0, "unknown"
        else:
            cost_val, src = cost, "gateway"
        return ChatResult(text=text, model=str(data.get("model") or model), usage=Usage(tin, tout, cost_val, src),
                          latency_ms=ms, raw=data, finish_reason=finish)
