"""Ways to reach the models: Vercel AI Gateway (one key for everything, with Jev) or each provider's own API.

`connect()` looks at the keys you have and returns a gateway plus the catalog it can actually use:

- AI_GATEWAY_API_KEY set: everything goes through Vercel, and Jev makes the decisions. Providers listed in
  SIAC_DIRECT (for example "anthropic,openai") go straight to their own API with their own key instead.
- No Vercel key, but provider keys (ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY, DEEPSEEK_API_KEY,
  DASHSCOPE_API_KEY): each model goes to its provider, only those providers are used, and the cheapest
  basic model you can reach answers the decision questions in Jev's place (less sharp, still cheap).
- SIAC_DECIDER=llm forces that stand-in decider even with a Vercel key (to compare it with Jev).
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from dataclasses import dataclass, replace
from typing import Any

import httpx

from . import __version__
from .catalog import Catalog, Model
from .gateway import (RETRY_STATUS, ChatResult, EvalResult, Gateway, GatewayError, Usage,
                      confidence_from_probs)


@dataclass(frozen=True)
class Provider:
    name: str
    env: tuple[str, ...]          # key variables, first found wins
    base_url: str
    style: str                    # "openai" or "anthropic"
    reasoning: dict[str, Any]     # how SIAC's "off"/"low" map to this API; empty: not sent


PROVIDERS: dict[str, Provider] = {
    "anthropic": Provider("anthropic", ("ANTHROPIC_API_KEY",), "https://api.anthropic.com/v1", "anthropic",
                          {"off": "low", "low": "low", "medium": "medium", "high": "high"}),
    "openai": Provider("openai", ("OPENAI_API_KEY",), "https://api.openai.com/v1", "openai",
                       {"off": "minimal", "low": "low", "medium": "medium", "high": "high"}),
    "google": Provider("google", ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
                       "https://generativelanguage.googleapis.com/v1beta/openai", "openai",
                       {"off": "none", "low": "low", "medium": "medium", "high": "high"}),
    "deepseek": Provider("deepseek", ("DEEPSEEK_API_KEY",), "https://api.deepseek.com/v1", "openai", {}),
    "alibaba": Provider("alibaba", ("DASHSCOPE_API_KEY",),
                        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "openai", {}),
}


def has_any_key() -> bool:
    return bool(os.environ.get("AI_GATEWAY_API_KEY")) or any(provider_key(n) for n in PROVIDERS)


def provider_key(name: str) -> str | None:
    p = PROVIDERS.get(name)
    if not p:
        return None
    return next((os.environ[v] for v in p.env if os.environ.get(v)), None)


def direct_id(model: Model) -> str:
    """The model's name on its provider's own API: the catalog's `direct_id`, else the id without the provider
    prefix (Anthropic writes versions with dashes: claude-opus-5.5 -> claude-opus-5-5)."""
    if model.direct_id:
        return model.direct_id
    name = model.id.split("/", 1)[-1]
    return name.replace(".", "-") if model.provider == "anthropic" else name


async def _post(client: httpx.AsyncClient, url: str, body: dict, headers: dict, timeout: float,
                 retries: int = 2) -> tuple[dict, int]:
    last: Exception | None = None
    for attempt in range(retries + 1):
        t0 = time.perf_counter()
        try:
            r = await client.post(url, json=body, headers=headers, timeout=timeout)
        except (httpx.TimeoutException, httpx.TransportError) as e:
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
        if attempt < retries:
            await asyncio.sleep(min(8.0, 0.6 * 2 ** attempt + random.random() * 0.3))
    if isinstance(last, GatewayError):
        raise last
    raise GatewayError(0, f"network error: {last}")


class DirectClient:
    """One provider's own API. Costs come from the catalog prices, because providers do not report them."""

    def __init__(self, provider: Provider, key: str, catalog: Catalog, client: httpx.AsyncClient,
                 base_url: str | None = None, timeout: float = 120.0):
        self.p, self.key, self.catalog, self.client, self.timeout = provider, key, catalog, client, timeout
        self.base_url = (base_url or os.environ.get(f"{provider.name.upper()}_BASE_URL") or provider.base_url).rstrip("/")

    async def chat(self, model: str, messages: list[dict], *, max_tokens: int | None = None,
                   temperature: float | None = None, json_mode: bool = False,
                   reasoning: str | None = None) -> ChatResult:
        m = self.catalog.get(model)
        effort = self.p.reasoning.get(reasoning or "", None)
        if self.p.style == "anthropic":
            return await self._anthropic(m, messages, max_tokens, temperature, json_mode, effort)
        return await self._openai(m, messages, max_tokens, temperature, json_mode, effort)

    async def _openai(self, m, messages, max_tokens, temperature, json_mode, effort) -> ChatResult:
        body: dict[str, Any] = {"model": direct_id(m), "messages": messages}
        if max_tokens:
            body["max_completion_tokens" if self.p.name == "openai" else "max_tokens"] = max_tokens
        if temperature is not None:
            body["temperature"] = temperature
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if effort and "haiku" not in m.id:
            body["reasoning_effort"] = effort
        headers = {"Authorization": f"Bearer {self.key}", "User-Agent": f"siac/{__version__}"}
        url = self.base_url + "/chat/completions"
        data, ms = await self._send(url, body, headers, ("response_format", "reasoning_effort", "temperature"))
        choice = (data.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content") or ""
        if isinstance(text, list):
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        u = data.get("usage") or {}
        tin, tout = int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
        return ChatResult(text=text, model=m.id, usage=Usage(tin, tout, m.cost(tin, tout), "catalog"),
                          latency_ms=ms, raw=data)

    async def _anthropic(self, m, messages, max_tokens, temperature, json_mode, effort) -> ChatResult:
        system = "\n\n".join(x["content"] for x in messages if x["role"] == "system")
        if json_mode:
            system = (system + "\n\n" if system else "") + "Reply with one valid JSON object and nothing else."
        body: dict[str, Any] = {"model": direct_id(m), "max_tokens": max_tokens or 4096,
                                "messages": [x for x in messages if x["role"] != "system"]}
        if system:
            body["system"] = system
        if temperature is not None:
            body["temperature"] = temperature
        if effort and "haiku" not in m.id:
            body["output_config"] = {"effort": effort}
        headers = {"x-api-key": self.key, "anthropic-version": "2023-06-01", "User-Agent": f"siac/{__version__}"}
        data, ms = await self._send(self.base_url + "/messages", body, headers, ("output_config", "temperature"))
        text = "".join(b.get("text", "") for b in data.get("content") or [] if b.get("type") == "text")
        u = data.get("usage") or {}
        tin = int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0)
        tout = int(u.get("output_tokens") or 0)
        return ChatResult(text=text, model=m.id, usage=Usage(tin, tout, m.cost(tin, tout), "catalog"),
                          latency_ms=ms, raw=data)

    async def _send(self, url, body, headers, optional) -> tuple[dict, int]:
        try:
            return await _post(self.client, url, body, headers, self.timeout)
        except GatewayError as e:
            if e.status in (400, 422) and any(k in body for k in optional):
                # some models refuse a control (effort, JSON mode, temperature): ask again without them
                for k in optional:
                    body.pop(k, None)
                return await _post(self.client, url, body, headers, self.timeout)
            raise


DECIDER_SYSTEM = (
    "You are a decision model. You read a STATE and answer QUESTIONS about it with calibrated probabilities. "
    "You never do the task in the state; you only decide. Reply with one JSON object and nothing else."
)


class LLMDecider:
    """Answers Jev's questions (choice, yes/no, score) with a cheap language model, when Jev is not reachable."""

    def __init__(self, chat, model: Model):
        self.chat, self.model = chat, model

    @staticmethod
    def _prompt(state: Any, questions: dict[str, dict]) -> str:
        lines = ["STATE:", state if isinstance(state, str) else json.dumps(state, ensure_ascii=False, indent=1),
                 "", "QUESTIONS:"]
        shape = {}
        for name, q in questions.items():
            if q["type"] == "boolean":
                lines.append(f'- "{name}" (yes or no): {q["instructions"]}')
                crit = q.get("criteria") or {}
                if isinstance(crit, dict) and crit:
                    lines += [f"    {k}: {v}" for k, v in crit.items()]
                shape[name] = {"probability": "<0 to 1 that the answer is yes>"}
            elif q["type"] == "choice":
                lines.append(f'- "{name}" (pick one option): {q["instructions"]}')
                lines += [f'    "{k}": {v}' for k, v in q["criteria"].items()]
                shape[name] = {"probabilities": {k: "<0 to 1>" for k in q["criteria"]}}
            else:
                lines.append(f'- "{name}" (score, 0 is the lowest level): {q["instructions"]}')
                lines += [f'    "{i}": {v}' for i, v in enumerate(q["criteria"])]
                shape[name] = {"probabilities": {str(i): "<0 to 1>" for i in range(len(q["criteria"]))}}
        lines += ["", "Reply with this JSON shape; probabilities of each question sum to 1:",
                  json.dumps(shape, ensure_ascii=False)]
        return "\n".join(lines)

    async def evaluate(self, state: Any, questions: dict[str, dict]) -> EvalResult:
        t0 = time.perf_counter()
        r = await self.chat(self.model.id, [{"role": "system", "content": DECIDER_SYSTEM},
                                            {"role": "user", "content": self._prompt(state, questions)}],
                            max_tokens=300 + 80 * len(questions), temperature=0, json_mode=True, reasoning="off")
        text = r.text
        try:
            data = json.loads(text[text.find("{"): text.rfind("}") + 1])
        except ValueError:
            data = {}
        answers, conf = {}, {}
        for name, q in questions.items():
            a = data.get(name) if isinstance(data.get(name), dict) else {}
            if q["type"] == "boolean":
                p = _num(a.get("probability"))
                answers[name] = {"type": "boolean", "probability": 0.5 if p is None else max(0.0, min(1.0, p))}
                continue
            keys = list(q["criteria"]) if q["type"] == "choice" else [str(i) for i in range(len(q["criteria"]))]
            raw = a.get("probabilities") if isinstance(a.get("probabilities"), dict) else {}
            probs = {k: max(0.0, _num(raw.get(k)) or 0.0) for k in keys}
            total = sum(probs.values())
            probs = {k: (v / total if total else 1 / len(keys)) for k, v in probs.items()}
            best = max(keys, key=lambda k: probs[k])
            if q["type"] == "choice":
                answers[name] = {"type": "choice", "choice": best, "probabilities": probs}
            else:
                answers[name] = {"type": "score", "score": float(best), "probabilities": probs}
            conf[name] = confidence_from_probs(probs) or 0.0
        return EvalResult(answers=answers, confidence=conf, usage=r.usage,
                          latency_ms=int((time.perf_counter() - t0) * 1000))


def _num(x: Any) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        if isinstance(x, str):
            m = re.search(r"\d*\.?\d+", x)
            return float(m.group()) if m else None
        return None


class MultiGateway:
    """Sends each model to Vercel or to its provider's own API, and each decision to Jev or the stand-in."""

    def __init__(self, catalog: Catalog, vercel: Gateway | None, direct: dict[str, DirectClient],
                 decider: LLMDecider | None, client: httpx.AsyncClient):
        self.catalog, self.vercel, self.direct, self.decider, self._client = catalog, vercel, direct, decider, client

    def route(self, model: str) -> str:
        provider = self.catalog.get(model).provider
        if provider in self.direct:
            return provider
        if self.vercel:
            return "vercel"
        raise GatewayError(401, f"no key for {provider}: set {' or '.join(PROVIDERS[provider].env)} "
                                "or AI_GATEWAY_API_KEY" if provider in PROVIDERS else f"no route to {model}")

    async def chat(self, model: str, messages: list[dict], **kw) -> ChatResult:
        way = self.route(model)
        if way == "vercel":
            return await self.vercel.chat(model, messages, **kw)
        return await self.direct[way].chat(model, messages, **kw)

    async def evaluate(self, state: Any, questions: dict[str, dict]) -> EvalResult:
        if self.decider:
            return await self.decider.evaluate(state, questions)
        return await self.vercel.evaluate(state, questions)

    async def aclose(self) -> None:
        if self.vercel:
            await self.vercel.aclose()
        await self._client.aclose()


def connect(catalog: Catalog) -> tuple[Any, Catalog]:
    """The gateway to use with the keys found in the environment, and the catalog restricted to what it reaches."""
    has_vercel = bool(os.environ.get("AI_GATEWAY_API_KEY"))
    keys = {name: k for name in PROVIDERS if (k := provider_key(name))}
    wanted = {p.strip() for p in os.environ.get("SIAC_DIRECT", "").split(",") if p.strip()}
    force_llm = os.environ.get("SIAC_DECIDER", "").lower() == "llm"
    if has_vercel and not wanted and not force_llm:
        return Gateway(catalog=catalog), catalog  # the simple, default path
    if not has_vercel and not keys:
        raise GatewayError(401, "No key found. Set AI_GATEWAY_API_KEY (one key for Jev and every model), or at "
                                "least one provider key such as ANTHROPIC_API_KEY or OPENAI_API_KEY. "
                                "Or run with --dry-run to see SIAC work without a key.")
    client = httpx.AsyncClient(timeout=120.0)
    direct_names = (wanted & set(keys)) if has_vercel else set(keys)
    direct = {n: DirectClient(PROVIDERS[n], keys[n], catalog, client) for n in sorted(direct_names)}
    vercel = Gateway(catalog=catalog) if has_vercel else None
    reach = None if has_vercel else set(direct)
    cat = catalog.reachable_only(reach) if reach is not None else catalog
    decider = None
    if force_llm or not has_vercel:
        model = cat.pick(1)
        decider = LLMDecider(None, model)
        cat = replace(cat, decider=replace(model, id=model.id, tiers=()), decider_label=f"{model.id} (stand-in "
                                                                                           "for Jev)")
    gw = MultiGateway(cat, vercel, direct, decider, client)
    if decider:
        decider.chat = gw.chat
    return gw, cat


async def doctor(catalog: Catalog) -> list[str]:
    """What SIAC can reach with the keys in this environment. Names of keys only, never their values."""
    out = []
    has_vercel = bool(os.environ.get("AI_GATEWAY_API_KEY"))
    keys = {n: k for n in PROVIDERS if (k := provider_key(n))}
    out.append("Vercel AI Gateway key: " + ("found" if has_vercel else "not set"))
    out.append("Provider keys: " + (", ".join(sorted(keys)) or "none"))
    try:
        gw, cat = connect(catalog)
    except GatewayError as e:
        return out + [f"Result: {e}"]
    direct = sorted(getattr(gw, "direct", {}) or {})
    if direct:
        out.append("Routes: " + ", ".join(f"{p} direct" for p in direct) +
                   ("; everything else through Vercel" if has_vercel else ""))
    else:
        out.append("Routes: everything through Vercel")
    out.append("Decider: " + (cat.decider_label or f"{cat.decider.id} (Jev)"))
    out.append("Providers in use: " + ", ".join(cat.providers("all")))
    out.append("Cheapest per tier: " + ", ".join(f"{t}: {cat.pick(t).id}" for t in sorted(cat.tiers)))
    async with httpx.AsyncClient(timeout=20) as c:
        if has_vercel:
            try:
                r = await c.get("https://ai-gateway.vercel.sh/v1/credits",
                                headers={"Authorization": f"Bearer {os.environ['AI_GATEWAY_API_KEY']}"})
                out.append(f"Vercel credit: {r.json().get('balance', '?')} USD" if r.status_code < 400
                           else f"Vercel credit: HTTP {r.status_code}")
            except httpx.HTTPError as e:
                out.append(f"Vercel credit: {e}")
        for name in direct:
            p, key = PROVIDERS[name], keys[name]
            base = (os.environ.get(f"{name.upper()}_BASE_URL") or p.base_url).rstrip("/")
            headers = ({"x-api-key": key, "anthropic-version": "2023-06-01"} if p.style == "anthropic"
                       else {"Authorization": f"Bearer {key}"})
            try:
                r = await c.get(base + "/models", headers=headers)
                if r.status_code >= 400:
                    out.append(f"{name}: HTTP {r.status_code} listing models (check the key)")
                    continue
                listed = {str(m.get("id", "")).removeprefix("models/") for m in r.json().get("data", [])}
            except (httpx.HTTPError, ValueError) as e:
                out.append(f"{name}: {e}")
                continue
            for m in (m for m in catalog.models if m.provider == name):
                mark = "ok" if direct_id(m) in listed else "NOT LISTED: set direct_id in models.yaml"
                out.append(f"  {m.id} -> {direct_id(m)}: {mark}")
    await gw.aclose()
    return out
