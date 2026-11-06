"""Device type catalog loaded from YAML (``config/devices.yaml``)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from monitoring_mqtt.core.topics import SEGMENT_PATTERN

DEFAULT_CATALOG_PATH = Path("config/devices.yaml")

Segment = Annotated[str, Field(pattern=SEGMENT_PATTERN)]
Finite = Annotated[float, Field(allow_inf_nan=False)]


class CatalogError(ValueError):
    """The catalog file is missing or invalid."""


class GeneratorSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["bounded_noise"] = "bounded_noise"
    center: Finite
    noise: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    minimum: Finite
    maximum: Finite
    decimals: Annotated[int, Field(ge=0, le=6)] = 2

    @model_validator(mode="after")
    def _check_range(self) -> GeneratorSpec:
        if not self.minimum <= self.center <= self.maximum:
            raise ValueError("center must lie within [minimum, maximum]")
        return self


class DeviceType(BaseModel):
    """Profile shared by every device of one type."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Segment
    domain: Segment
    metric: Segment
    unit: str | None
    site_prefix: Segment
    generator: GeneratorSpec


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
