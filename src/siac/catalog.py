"""Model catalog: which models exist, what they cost and which tier each one covers."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from importlib import resources
from pathlib import Path
from typing import Iterable

import yaml

# Typical request used to rank models by price: 1,500 tokens in, 600 out.
TYPICAL_IN, TYPICAL_OUT = 1500, 600


@dataclass(frozen=True)
class Model:
    id: str
    provider: str
    tiers: tuple[int, ...]
    price_in: float  # USD per million input tokens
    price_out: float  # USD per million output tokens
    context: int
    vision: bool = False
    direct_id: str = ""  # name on the provider's own API, when it differs from the id without the prefix

    def cost(self, tokens_in: int, tokens_out: int) -> float:
        return (tokens_in * self.price_in + tokens_out * self.price_out) / 1e6

    @property
    def typical_cost(self) -> float:
        return self.cost(TYPICAL_IN, TYPICAL_OUT)


class NoModelError(RuntimeError):
    """No model in the catalog fits the request (tier, providers, context)."""


@dataclass
class Catalog:
    decider: Model
    planner: str
    baseline: str
    tiers: dict[int, dict]
    task_types: dict[str, str]
    models: list[Model]
    planner_light: str = ""
    profiles: dict[str, list[str]] = field(default_factory=dict)
    source: str = ""
    reachable: frozenset[str] | None = None  # providers the current keys reach; None: all of them
    min_tier: dict[str, int] = field(default_factory=dict)  # task type -> lowest tier allowed
    decider_label: str = ""

    # ------------------------------------------------------------------ loading
    @classmethod
    def load(cls, path: str | Path | None = None) -> "Catalog":
        if path is None:
            text = resources.files("siac").joinpath("models.yaml").read_text(encoding="utf-8")
            source = "built-in models.yaml"
        else:
            text = Path(path).read_text(encoding="utf-8")
            source = str(path)
        return cls.from_dict(yaml.safe_load(text), source=source)

    @classmethod
    def from_dict(cls, data: dict, source: str = "") -> "Catalog":
        d = data["decider"]
        decider = Model(
            id=d["id"], provider=d["id"].split("/")[0], tiers=(), context=int(d.get("context", 32000)),
            price_in=float(d["price"]["input"]), price_out=float(d["price"].get("output", 0.0)),
        )
        models = [
            Model(
                id=m["id"],
                provider=m.get("provider", m["id"].split("/")[0]),
                tiers=tuple(int(t) for t in m["tiers"]),
                price_in=float(m["price"]["input"]),
                price_out=float(m["price"]["output"]),
                context=int(m.get("context", 128000)),
                vision=bool(m.get("vision", False)),
                direct_id=str(m.get("direct_id") or ""),
            )
            for m in data["models"]
        ]
        ids = {m.id for m in models}
        for role in ("planner", "baseline", "planner_light"):
            if role in data and data[role] not in ids:
                raise ValueError(f"{role} model {data[role]!r} is not in the models list")
        return cls(
            decider=decider,
            planner=data["planner"],
            baseline=data["baseline"],
            tiers={int(k): v for k, v in data["tiers"].items()},
            task_types=dict(data["task_types"]),
            models=models,
            profiles={k: list(v) for k, v in (data.get("profiles") or {}).items()},
            planner_light=data.get("planner_light") or data["planner"],
            source=source,
            min_tier={str(k): int(v) for k, v in (data.get("min_tier") or {}).items()},
        )

    # ------------------------------------------------------------------ queries
    def get(self, model_id: str) -> Model:
        if model_id == self.decider.id:
            return self.decider
        for m in self.models:
            if m.id == model_id:
                return m
        raise KeyError(model_id)

    def reachable_only(self, providers: set[str]) -> "Catalog":
        """The same catalog, limited to the providers the available keys can reach."""
        return replace(self, reachable=frozenset(providers))

    def providers(self, profile: str = "all") -> list[str]:
        if profile in self.profiles:
            found = self.profiles[profile]
        elif profile == "all":
            found = sorted({m.provider for m in self.models})
        else:
            raise KeyError(f"unknown profile {profile!r}; known: {', '.join(self.profiles)}")
        return [p for p in found if self.reachable is None or p in self.reachable]

    def candidates(self, tier: int, providers: Iterable[str], *, vision: bool = False,
                   min_context: int = 0) -> list[Model]:
        allowed = set(providers)
        found = [
            m for m in self.models
            if tier in m.tiers and m.provider in allowed and (m.vision or not vision) and m.context >= min_context
        ]
        # Cheapest first; on a tie, the order in the file wins (sort is stable).
        return sorted(found, key=lambda m: m.typical_cost)

    def pick(self, tier: int, profile: str = "all", *, vision: bool = False, min_context: int = 0) -> Model:
        """Cheapest model for the tier. If no model covers it, go up one tier at a time."""
        providers = self.providers(profile)
        order = list(range(max(1, tier), 5)) + list(range(min(4, tier) - 1, 0, -1))  # up first, then down
        for t in order:
            found = self.candidates(t, providers, vision=vision, min_context=min_context)
            if found:
                return found[0]
        raise NoModelError(f"no model for tier {tier} with profile {profile!r}")

    def chain(self, tier: int, profile: str = "all", *, vision: bool = False, min_context: int = 0,
              length: int = 3, max_ratio: float = 1.5) -> list[Model]:
        """Fallback list for when a provider refuses or fails: the cheapest model of the tier first, then other
        models of the same tier that cost at most `max_ratio` times as much, then the best of the lower tiers.
        A fallback never costs much more than the first choice."""
        providers = self.providers(profile)
        first = self.pick(tier, profile, vision=vision, min_context=min_context)
        real_tier = next(t for t in list(range(max(1, tier), 5)) + list(range(min(4, tier) - 1, 0, -1))
                         if first in self.candidates(t, providers, vision=vision, min_context=min_context))
        out = [first]
        # a free (local) first choice must not stop the fallbacks: measure against the cheapest paid model
        paid = [m for m in self.candidates(real_tier, providers, vision=vision, min_context=min_context)
                if m.typical_cost > 0]
        ceiling = max(first.typical_cost, paid[0].typical_cost if paid else 0.0) * max_ratio
        for m in self.candidates(real_tier, providers, vision=vision, min_context=min_context)[1:]:
            if m.typical_cost <= ceiling and m not in out:
                out.append(m)
        for t in range(real_tier - 1, 0, -1):
            for m in self.candidates(t, providers, vision=vision, min_context=min_context)[:1]:
                if m not in out:
                    out.append(m)
        return out[:length]

    def pick_named(self, model_id: str, profile: str = "all", fallback_tier: int = 3) -> Model:
        """A fixed role model (planner, baseline) if the profile allows its provider, else the cheapest of a tier."""
        m = self.get(model_id)
        if m.provider in self.providers(profile):
            return m
        return self.pick(fallback_tier, profile)
