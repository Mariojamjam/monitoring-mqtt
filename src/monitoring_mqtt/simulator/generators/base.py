"""Measurement generators.

Every generator produces one value per call and can be *steered*: ``steer(target)`` moves
the value the generator tends to (used for anomalies), and ``steer(None)`` restores its
normal mean. All randomness comes from a ``random.Random`` seeded per device, so a run is
reproducible from the experiment seed.
"""

from __future__ import annotations

import random
from typing import Protocol


class MeasurementGenerator(Protocol):
    def next_value(self) -> float: ...

    def steer(self, target: float | None) -> None: ...


def seeded_rng(seed: int, device_id: str, stream: str = "") -> random.Random:
    """Deterministic per-device random stream derived from a base seed.

    ``stream`` separates independent uses (values, site offset, episodes), so enabling
    one feature never changes the random numbers another one draws.
    """
    suffix = f":{stream}" if stream else ""
    return random.Random(f"{seed}:{device_id}{suffix}")


def _clip(value: float, minimum: float, maximum: float, decimals: int) -> float:
    return round(min(max(value, minimum), maximum), decimals)


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
        self._target: float | None = None

    def steer(self, target: float | None) -> None:
        self._target = target

    def next_value(self) -> float:
        center = self.center if self._target is None else self._target
        value = center + self._rng.uniform(-self.noise, self.noise)
        return _clip(value, self.minimum, self.maximum, self.decimals)


class MeanRevertingGenerator:
    """Values that drift with gaussian noise and are pulled back towards ``mean``.

    ``x_next = clip(x + alpha * (target - x) + gauss(0, noise), minimum, maximum)``.
    ``alpha`` is the share of the distance to the target recovered at each step: 1 jumps
    straight to the target, small values give slow, smooth changes. Steering moves the
    target, so a value leaves its normal band gradually and comes back the same way.
    """

    def __init__(
        self,
        mean: float,
        alpha: float,
        noise: float,
        minimum: float,
        maximum: float,
        rng: random.Random,
        decimals: int = 2,
    ) -> None:
        if not minimum <= mean <= maximum:
            raise ValueError("mean must lie within [minimum, maximum]")
        if not 0 < alpha <= 1:
            raise ValueError("alpha must be in (0, 1]")
        if noise < 0:
            raise ValueError("noise must be non-negative")
        self.mean = mean
        self.alpha = alpha
        self.noise = noise
        self.minimum = minimum
        self.maximum = maximum
        self.decimals = decimals
        self._rng = rng
        self._target: float | None = None
        self._value = mean

    def steer(self, target: float | None) -> None:
        self._target = target

    def next_value(self) -> float:
        target = self.mean if self._target is None else self._target
        step = self.alpha * (target - self._value) + self._rng.gauss(0.0, self.noise)
        self._value = min(max(self._value + step, self.minimum), self.maximum)
        return _clip(self._value, self.minimum, self.maximum, self.decimals)
