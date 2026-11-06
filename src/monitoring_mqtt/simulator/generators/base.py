"""Generator contract and the bounded-noise base implementation."""

from __future__ import annotations

import random
from typing import Protocol


class MeasurementGenerator(Protocol):
    def next_value(self) -> float: ...


def seeded_rng(seed: int, device_id: str) -> random.Random:
    """Deterministic per-device random stream derived from a base seed."""
    return random.Random(f"{seed}:{device_id}")


class BoundedNoiseGenerator:
    """Values around ``center`` with uniform noise, clipped to ``[minimum, maximum]``."""

    def __init__(
        self,
        center: float,
        noise: float,
        minimum: float,
        maximum: float,
        rng: random.Random,
        decimals: int = 2,
    ) -> None:
        if not minimum <= center <= maximum:
            raise ValueError("center must lie within [minimum, maximum]")
        if noise < 0:
            raise ValueError("noise must be non-negative")
        self.center = center
        self.noise = noise
        self.minimum = minimum
        self.maximum = maximum
        self.decimals = decimals
        self._rng = rng

    def next_value(self) -> float:
        value = self.center + self._rng.uniform(-self.noise, self.noise)
        return round(min(max(value, self.minimum), self.maximum), self.decimals)
