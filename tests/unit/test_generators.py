import itertools
import random
import statistics

import pytest

from monitoring_mqtt.simulator.generators import BoundedNoiseGenerator, MeanRevertingGenerator
from monitoring_mqtt.simulator.generators.base import seeded_rng


def test_values_are_clipped_to_bounds() -> None:
    # Noise far larger than the range forces clipping on both sides.
    gen = BoundedNoiseGenerator(5.0, 10.0, 4.0, 6.0, random.Random(1))
    values = [gen.next_value() for _ in range(500)]
    assert min(values) == 4.0
    assert max(values) == 6.0


def mean_reverting(rng_seed: int = 1, **overrides) -> MeanRevertingGenerator:
    params = {"mean": 6.5, "alpha": 0.3, "noise": 0.2, "minimum": 2.0, "maximum": 12.0}
    params.update(overrides)
    return MeanRevertingGenerator(**params, rng=random.Random(rng_seed))


def test_mean_reverting_stays_near_its_mean_and_changes_smoothly() -> None:
    gen = mean_reverting()
    values = [gen.next_value() for _ in range(2000)]
    assert abs(statistics.fmean(values) - 6.5) < 0.05
    steps = [abs(b - a) for a, b in itertools.pairwise(values)]
    assert max(steps) < 1.5  # no independent jumps across the whole range


def test_mean_reverting_is_clipped_to_bounds() -> None:
    gen = mean_reverting(noise=50.0)
    values = [gen.next_value() for _ in range(500)]
    assert min(values) == 2.0 and max(values) == 12.0


def test_steering_moves_values_gradually_and_back() -> None:
    gen = mean_reverting(noise=0.0)
    gen.steer(10.0)
    out = [gen.next_value() for _ in range(6)]
    assert out == sorted(out) and 6.5 < out[0] < out[-1] < 10.0  # ramps, never jumps
    assert out[-1] > 9.0
    gen.steer(None)
    back = [gen.next_value() for _ in range(20)]
    assert back == sorted(back, reverse=True) and abs(back[-1] - 6.5) < 0.01


def test_bounded_noise_can_be_steered() -> None:
    gen = BoundedNoiseGenerator(5.0, 0.0, 0.0, 10.0, random.Random(1))
    gen.steer(9.0)
    assert gen.next_value() == 9.0
    gen.steer(None)
    assert gen.next_value() == 5.0


@pytest.mark.parametrize(
    "overrides", [{"alpha": 0.0}, {"alpha": 1.5}, {"noise": -1.0}, {"mean": 20.0}]
)
def test_mean_reverting_rejects_invalid_parameters(overrides: dict) -> None:
    with pytest.raises(ValueError):
        mean_reverting(**overrides)


def test_random_streams_are_independent() -> None:
    values = seeded_rng(42, "dev-1").random()
    assert seeded_rng(42, "dev-1").random() == values  # same stream, same numbers
    assert seeded_rng(42, "dev-1", "episodes").random() != values
