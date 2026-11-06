import random

from monitoring_mqtt.simulator.generators import BoundedNoiseGenerator


def test_values_are_clipped_to_bounds() -> None:
    # Noise far larger than the range forces clipping on both sides.
    gen = BoundedNoiseGenerator(5.0, 10.0, 4.0, 6.0, random.Random(1))
    values = [gen.next_value() for _ in range(500)]
    assert min(values) == 4.0
    assert max(values) == 6.0
