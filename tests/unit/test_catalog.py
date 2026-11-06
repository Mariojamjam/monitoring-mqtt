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


def test_duplicate_names_rejected(tmp_path: Path) -> None:
    item = VALID.split("device_types:\n")[1]
    with pytest.raises(CatalogError):
        load_catalog(write(tmp_path, VALID + item))


@pytest.mark.parametrize("content", [None, "a: [unclosed"])
def test_missing_or_malformed_file(tmp_path: Path, content: str | None) -> None:
    path = tmp_path / "missing.yaml" if content is None else write(tmp_path, content)
    with pytest.raises(CatalogError):
        load_catalog(path)
