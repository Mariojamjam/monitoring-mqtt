"""What the devices measure and when they alert: the reference for charts."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter

from monitoring_mqtt.api import schemas
from monitoring_mqtt.api.deps import CatalogDep
from monitoring_mqtt.core.rules import RulesFile, load_configured_rules
from monitoring_mqtt.simulator.catalog import DEFAULT_CATALOG_PATH, DeviceCatalog, load_catalog

router = APIRouter(tags=["catalog"])


def build_catalog(devices: DeviceCatalog, rules: RulesFile) -> schemas.Catalog:
    types = [
        schemas.DeviceTypeInfo(
            name=t.name,
            domain=t.domain,
            metric=t.metric,
            unit=t.unit,
            bounds=schemas.Band(minimum=t.generator.minimum, maximum=t.generator.maximum),
            normal=None if t.operating_range is None else schemas.Band(
                minimum=t.operating_range.minimum, maximum=t.operating_range.maximum),
            anomaly_low=t.anomaly.low if t.anomaly else None,
            anomaly_high=t.anomaly.high if t.anomaly else None,
        )
        for t in devices.device_types
    ]  # fmt: skip
    alert_rules = [
        schemas.AlertRuleInfo(
            id=r.id,
            device_type=r.device_type,
            severity=r.severity.value,
            condition=r.condition.value,
            threshold=r.threshold,
            clear_at=r.clear_level,
            after=r.after,
            clear_after=r.clear_after,
        )
        for r in rules.rules
    ]
    return schemas.Catalog(device_types=types, alert_rules=alert_rules)


def load_configured_catalog() -> schemas.Catalog:
    """The device catalog (``DEVICES_CONFIG``) and the rules the alert engine applies
    (``ALERT_RULES``, else the rules file)."""
    path = Path(os.environ.get("DEVICES_CONFIG", str(DEFAULT_CATALOG_PATH)))
    return build_catalog(load_catalog(path), load_configured_rules())


@router.get("/catalog", summary="Device types and alert rules")
def catalog(info: CatalogDep) -> schemas.Catalog:
    """Every device type with its value bounds, normal band and anomaly targets, and the
    alert rules of the current experiment. Dashboards draw them on the charts. Static: it
    does not need the database."""
    return info
