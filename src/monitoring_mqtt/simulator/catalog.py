"""Device type catalog loaded from YAML (``config/devices.yaml``).

A device type defines what a device *is*: its topic segments, how its value evolves
(``generator``), the synthetic normal band (``operating_range``) and where its values go
during an anomaly (``anomaly``). What happens during an experiment (how often anomalies
occur, faults) is defined in the experiment file, not here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    ValidationError,
    model_validator,
)

from monitoring_mqtt.core.topics import SEGMENT_PATTERN

DEFAULT_CATALOG_PATH = Path("config/devices.yaml")

Segment = Annotated[str, Field(pattern=SEGMENT_PATTERN)]
Finite = Annotated[float, Field(allow_inf_nan=False)]
NonNegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Decimals = Annotated[int, Field(ge=0, le=6)]


class CatalogError(ValueError):
    """The catalog file is missing or invalid."""


class _Spec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class BoundedNoiseSpec(_Spec):
    """Independent values: ``center`` plus uniform noise, clipped to the bounds."""

    kind: Literal["bounded_noise"] = "bounded_noise"
    center: Finite
    noise: NonNegative
    minimum: Finite
    maximum: Finite
    decimals: Decimals = 2

    @property
    def mean(self) -> float:
        return self.center

    @model_validator(mode="after")
    def _check_range(self) -> BoundedNoiseSpec:
        if not self.minimum <= self.center <= self.maximum:
            raise ValueError("center must lie within [minimum, maximum]")
        return self


class MeanRevertingSpec(_Spec):
    """Values that drift and are pulled back to ``mean``.

    ``x_next = clip(x + alpha * (target - x) + gauss(0, noise), minimum, maximum)``, where
    ``target`` is the device's own mean: ``mean`` shifted by up to ``site_spread``, so that
    devices of the same type differ slightly, like different ponds or barns.
    """

    kind: Literal["mean_reverting"]
    mean: Finite
    alpha: Annotated[float, Field(gt=0, le=1, allow_inf_nan=False)]
    noise: NonNegative
    minimum: Finite
    maximum: Finite
    site_spread: NonNegative = 0.0
    decimals: Decimals = 2

    @model_validator(mode="after")
    def _check_range(self) -> MeanRevertingSpec:
        if not self.minimum <= self.mean - self.site_spread:
            raise ValueError("mean - site_spread must not be below minimum")
        if not self.mean + self.site_spread <= self.maximum:
            raise ValueError("mean + site_spread must not be above maximum")
        return self


def _generator_kind(value: object) -> str:
    """``kind`` selects the generator; catalogs written before it existed mean bounded noise."""
    if isinstance(value, dict):
        return str(value.get("kind", "bounded_noise"))
    return str(getattr(value, "kind", "bounded_noise"))


GeneratorSpec = Annotated[
    Annotated[BoundedNoiseSpec, Tag("bounded_noise")]
    | Annotated[MeanRevertingSpec, Tag("mean_reverting")],
    Discriminator(_generator_kind),
]


class Range(_Spec):
    minimum: Finite
    maximum: Finite

    @model_validator(mode="after")
    def _ordered(self) -> Range:
        if self.minimum >= self.maximum:
            raise ValueError("minimum must be below maximum")
        return self

    def contains(self, value: float) -> bool:
        return self.minimum <= value <= self.maximum


class AnomalyTargets(_Spec):
    """Values an anomaly drives the device towards: ``low``, ``high`` or both."""

    low: Finite | None = None
    high: Finite | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> AnomalyTargets:
        if self.low is None and self.high is None:
            raise ValueError("set low, high or both")
        return self

    def target(self, direction: str) -> float | None:
        return self.high if direction == "high" else self.low

    @property
    def directions(self) -> list[str]:
        return [d for d in ("high", "low") if self.target(d) is not None]


class DeviceType(BaseModel):
    """Profile shared by every device of one type."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Segment
    domain: Segment
    metric: Segment
    unit: str | None
    site_prefix: Segment
    generator: GeneratorSpec
    operating_range: Range | None = None
    anomaly: AnomalyTargets | None = None

    @model_validator(mode="after")
    def _consistent(self) -> DeviceType:
        gen = self.generator
        band = self.operating_range
        if band and not (gen.minimum <= band.minimum and band.maximum <= gen.maximum):
            raise ValueError("operating_range must lie within the generator bounds")
        if self.anomaly is None:
            return self
        if band is None:
            raise ValueError("anomaly targets need an operating_range")
        low, high = self.anomaly.low, self.anomaly.high
        if low is not None and not gen.minimum <= low < band.minimum:
            raise ValueError("anomaly.low must be below operating_range and within bounds")
        if high is not None and not band.maximum < high <= gen.maximum:
            raise ValueError("anomaly.high must be above operating_range and within bounds")
        return self


class DeviceCatalog(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1]
    device_types: Annotated[list[DeviceType], Field(min_length=1)]

    @model_validator(mode="after")
    def _unique_names(self) -> DeviceCatalog:
        names = [t.name for t in self.device_types]
        if len(names) != len(set(names)):
            raise ValueError("device type names must be unique")
        return self

    def get(self, name: str) -> DeviceType | None:
        return next((t for t in self.device_types if t.name == name), None)


def load_catalog(path: Path = DEFAULT_CATALOG_PATH) -> DeviceCatalog:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CatalogError(f"cannot read device catalog {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise CatalogError(f"invalid YAML in {path}: {exc}") from exc
    try:
        return DeviceCatalog.model_validate(data)
    except ValidationError as exc:
        raise CatalogError(f"invalid device catalog {path}: {exc}") from exc
