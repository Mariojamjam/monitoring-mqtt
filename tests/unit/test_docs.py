"""The documentation stays consistent: generated references are current, links resolve."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from monitoring_mqtt.cli.reference import markdown

REPO = Path(__file__).resolve().parents[2]
DOCS = [REPO / "README.md", *sorted((REPO / "docs").rglob("*.md"))]

FENCE = re.compile(r"^(```|~~~).*?^\1", re.MULTILINE | re.DOTALL)
CODE_SPAN = re.compile(r"`[^`\n]*`")
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)
EXTERNAL = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)


def prose(text: str) -> str:
    """The text without code blocks and code spans, where brackets are not links."""
    return CODE_SPAN.sub("", FENCE.sub("", text))


def anchors(path: Path) -> set[str]:
    """GitHub's heading anchors: lowercase, punctuation dropped, spaces to hyphens."""
    seen: dict[str, int] = {}
    result = set()
    for heading in HEADING.findall(FENCE.sub("", path.read_text(encoding="utf-8"))):
        text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading).replace("`", "")
        slug = re.sub(r"[^\w\- ]", "", text.lower()).replace(" ", "-")
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        result.add(slug if count == 0 else f"{slug}-{count}")
    return result


def broken_links(path: Path) -> list[str]:
    broken = []
    for target in LINK.findall(prose(path.read_text(encoding="utf-8"))):
        if EXTERNAL.match(target):
            continue
        file_part, _, anchor = target.partition("#")
        linked = (path.parent / file_part).resolve() if file_part else path
        if not linked.exists():
            broken.append(f"{target}: no such file")
        elif anchor and linked.suffix == ".md" and anchor not in anchors(linked):
            broken.append(f"{target}: no such heading")
    return broken


@pytest.mark.parametrize("path", DOCS, ids=lambda p: str(p.relative_to(REPO)))
def test_relative_links_resolve(path: Path) -> None:
    assert broken_links(path) == []


def test_anchor_slugs_follow_github() -> None:
    sample = REPO / "docs" / "design" / "design.md"
    assert "2-communication-map" in anchors(sample)


def test_committed_cli_reference_is_up_to_date() -> None:
    committed = (REPO / "docs" / "reference" / "cli.md").read_text(encoding="utf-8")
    assert committed == markdown(), (
        "docs/reference/cli.md is out of date: run "
        "`uv run python -m monitoring_mqtt.cli.reference > docs/reference/cli.md`"
    )
