"""Builds the virtual devices hosted by one worker."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable

from monitoring_mqtt.core.config import MqttSettings
from monitoring_mqtt.simulator.catalog import (
    BoundedNoiseSpec,
    DeviceCatalog,
    DeviceType,
    GeneratorSpec,
)
from monitoring_mqtt.simulator.device import DeviceProfile, VirtualDevice
from monitoring_mqtt.simulator.generators.base import (
    BoundedNoiseGenerator,
    MeanRevertingGenerator,
    MeasurementGenerator,
    seeded_rng,
)
from monitoring_mqtt.simulator.identity import device_id


def device_mean(spec: GeneratorSpec, seed: int, dev_id: str) -> float:
    """The mean of one device: the type's mean shifted by up to ``site_spread``."""
    if isinstance(spec, BoundedNoiseSpec) or spec.site_spread == 0:
        return spec.mean
    offset = seeded_rng(seed, dev_id, "site").uniform(-spec.site_spread, spec.site_spread)
    return round(spec.mean + offset, spec.decimals)


def build_generator(spec: GeneratorSpec, seed: int, dev_id: str) -> MeasurementGenerator:
    """Create the generator for one device; its random stream depends on seed + device id."""
    rng = seeded_rng(seed, dev_id)
    if isinstance(spec, BoundedNoiseSpec):
        return BoundedNoiseGenerator(
            center=spec.center,
            noise=spec.noise,
            minimum=spec.minimum,
            maximum=spec.maximum,
            rng=rng,
            decimals=spec.decimals,
        )
    return MeanRevertingGenerator(
        mean=device_mean(spec, seed, dev_id),
        alpha=spec.alpha,
        noise=spec.noise,
        minimum=spec.minimum,
        maximum=spec.maximum,
        rng=rng,
        decimals=spec.decimals,
    )


def select_types(catalog: DeviceCatalog, names: list[str] | None) -> list[DeviceType]:
    """Catalog types to host, in catalog order; ``None`` or empty means all types."""
    if not names:
        return list(catalog.device_types)
    known = {t.name for t in catalog.device_types}
    unknown = sorted(set(names) - known)
    if unknown:
        raise ValueError(f"unknown device type(s) {unknown}; valid types: {sorted(known)}")
    return [t for t in catalog.device_types if t.name in names]


def assign_types(types: list[DeviceType], count: int) -> list[DeviceType]:
    """Round-robin over ``types``: index 0 → first type, 1 → second, ..."""
    return [types[i % len(types)] for i in range(count)]


def build_devices(
    types: list[DeviceType],
    count: int,
    worker_id: str,
    run_id: str,
    seed: int,
    settings: MqttSettings,
    device_factory: Callable[..., VirtualDevice] = VirtualDevice,
) -> list[VirtualDevice]:
    """Create ``count`` devices with ids ``{domain}-{worker_id}-{index:03d}`` (index from 1).

    ``site_id`` numbers devices per type: the first oxygen probe is ``pond-01``, the second
    ``pond-02``, and so on.
    """
    per_type: Counter[str] = Counter()
    devices = []
    for index, device_type in enumerate(assign_types(types, count), start=1):
        per_type[device_type.name] += 1
        dev_id = device_id(device_type.domain, worker_id, index)
        devices.append(
            device_factory(
                device_id=dev_id,
                profile=DeviceProfile(
                    device_type=device_type.name,
                    domain=device_type.domain,
                    metric=device_type.metric,
                    unit=device_type.unit,
                ),
                site_id=f"{device_type.site_prefix}-{per_type[device_type.name]:02d}",
                run_id=run_id,
                generator=build_generator(device_type.generator, seed, dev_id),
                settings=settings,
            )
        )
    return devices
