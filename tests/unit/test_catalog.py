from pathlib import Path

import pytest

from monitoring_mqtt.simulator.catalog import CatalogError, load_catalog

VALID = """
schema_version: 1
device_types:
  - name: probe
    domain: aquaculture
    metric: dissolved_oxygen
    unit: mg/L
    site_prefix: pond
    generator: {center: 6.5, noise: 1.0, minimum: 3.0, maximum: 10.0}
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "devices.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_valid_catalog_loads(tmp_path: Path) -> None:
    assert [t.name for t in load_catalog(write(tmp_path, VALID)).device_types] == ["probe"]


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("center: 6.5", "center: 11.0"),  # center outside [minimum, maximum]
        ("domain: aquaculture", "domain: Aqua/Culture"),  # not a valid topic segment
        ("schema_version: 1", "schema_version: 2"),
        ("unit: mg/L", "unit: mg/L\n    extra: 1"),  # typo protection
    ],
)
def test_invalid_catalogs(tmp_path: Path, old: str, new: str) -> None:
    with pytest.raises(CatalogError):
        load_catalog(write(tmp_path, VALID.replace(old, new)))


MEAN_REVERTING = """
schema_version: 1
device_types:
  - name: probe
    domain: aquaculture
    metric: dissolved_oxygen
    unit: mg/L
    site_prefix: pond
    generator:
      {kind: mean_reverting, mean: 6.5, alpha: 0.3, noise: 0.2, site_spread: 0.4,
       minimum: 2.0, maximum: 12.0}
    operating_range: {minimum: 5.0, maximum: 8.0}
    anomaly: {low: 3.0, high: 10.0}
"""


def test_mean_reverting_type_with_range_and_anomaly(tmp_path: Path) -> None:
    probe = load_catalog(write(tmp_path, MEAN_REVERTING)).device_types[0]
    assert probe.generator.kind == "mean_reverting"
    assert probe.operating_range is not None and probe.operating_range.contains(6.5)
    assert probe.anomaly is not None and probe.anomaly.directions == ["high", "low"]


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("alpha: 0.3", "alpha: 0"),
        ("kind: mean_reverting", "kind: random_walk"),
        ("site_spread: 0.4", "site_spread: 6.0"),  # a device mean could leave the bounds
        ("high: 10.0", "high: 7.0"),  # anomaly inside the operating range
        ("low: 3.0", "low: 1.0"),  # anomaly outside the generator bounds
        ("{minimum: 5.0, maximum: 8.0}", "{minimum: 8.0, maximum: 5.0}"),
        ("anomaly: {low: 3.0, high: 10.0}", "anomaly: {}"),
        ("    operating_range: {minimum: 5.0, maximum: 8.0}\n", ""),  # anomaly needs a band
    ],
)
def test_invalid_mean_reverting_types(tmp_path: Path, old: str, new: str) -> None:
    assert old in MEAN_REVERTING
    with pytest.raises(CatalogError):
        load_catalog(write(tmp_path, MEAN_REVERTING.replace(old, new)))


def test_shipped_catalog_is_valid() -> None:
    catalog = load_catalog(Path(__file__).parents[2] / "config" / "devices.yaml")
    assert all(t.operating_range and t.anomaly for t in catalog.device_types)


def test_duplicate_names_rejected(tmp_path: Path) -> None:
    item = VALID.split("device_types:\n")[1]
    with pytest.raises(CatalogError):
        load_catalog(write(tmp_path, VALID + item))


@pytest.mark.parametrize("content", [None, "a: [unclosed"])
def test_missing_or_malformed_file(tmp_path: Path, content: str | None) -> None:
    path = tmp_path / "missing.yaml" if content is None else write(tmp_path, content)
    with pytest.raises(CatalogError):
        load_catalog(path)
