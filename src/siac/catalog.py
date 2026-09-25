"""Model catalog: which models exist, what they cost and which tier each one covers."""

from __future__ import annotations

from dataclasses import dataclass, field
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
    profiles: dict[str, list[str]] = field(default_factory=dict)
    source: str = ""

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
            )
            for m in data["models"]
        ]
        ids = {m.id for m in models}
        for role in ("planner", "baseline"):
            if data[role] not in ids:
                raise ValueError(f"{role} model {data[role]!r} is not in the models list")
        return cls(
            decider=decider,
            planner=data["planner"],
            baseline=data["baseline"],
            tiers={int(k): v for k, v in data["tiers"].items()},
            task_types=dict(data["task_types"]),
            models=models,
            profiles={k: list(v) for k, v in (data.get("profiles") or {}).items()},
            source=source,
        )

    # ------------------------------------------------------------------ queries
    def get(self, model_id: str) -> Model:
        if model_id == self.decider.id:
            return self.decider
        for m in self.models:
            if m.id == model_id:
                return m
        raise KeyError(model_id)

    def providers(self, profile: str = "all") -> list[str]:
        if profile in self.profiles:
            return self.profiles[profile]
        if profile == "all":
            return sorted({m.provider for m in self.models})
        raise KeyError(f"unknown profile {profile!r}; known: {', '.join(self.profiles)}")

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
        for t in range(max(1, tier), 5):
            found = self.candidates(t, providers, vision=vision, min_context=min_context)
            if found:
                return found[0]
        raise NoModelError(f"no model for tier {tier} with profile {profile!r}")

    def pick_named(self, model_id: str, profile: str = "all", fallback_tier: int = 3) -> Model:
        """A fixed role model (planner, baseline) if the profile allows its provider, else the cheapest of a tier."""
        m = self.get(model_id)
        if m.provider in self.providers(profile):
            return m
        return self.pick(fallback_tier, profile)
