"""Query parameters shared by the read routes."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import Query

MAX_PAGE = 1000

Limit = Annotated[int, Query(ge=1, le=MAX_PAGE, description="Items per page (1-1000)")]
Cursor = Annotated[
    str | None, Query(description="`next_cursor` of the previous page (omit for the first)")
]
Since = Annotated[
    datetime | None,
    Query(alias="from", description="Start, inclusive (ISO 8601 with time zone)"),
]
Until = Annotated[
    datetime | None, Query(alias="to", description="End, exclusive (ISO 8601 with time zone)")
]
NOT_FOUND = {404: {"description": "Not found"}}
UNAVAILABLE = {503: {"description": "Database unavailable; retry later"}}
