"""What every page shares: the API (cached), the sidebar filters, live refresh, outages.

Streamlit runs the page script again on every interaction. Two things keep that cheap:

- API answers are cached for a couple of seconds (``st.cache_data``), keyed by the query,
  so reruns and several open browsers share one request;
- live blocks are fragments (``st.fragment(run_every=...)``): only they run again every
  few seconds, not the whole page.

When the API cannot answer, pages show a warning and keep the last answer they got.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import streamlit as st

from monitoring_mqtt.dashboard.client import ApiClient, ApiUnavailableError
from monitoring_mqtt.dashboard.data import WINDOWS, Window, make_window
from monitoring_mqtt.dashboard.settings import DashboardSettings

CACHE_TTL_SECONDS = 2
CATALOG_TTL_SECONDS = 60
API_KEY = "api"  # session state: tests put a fake API here
ALL = "All"
DATETIME_FORMAT = "YYYY-MM-DD HH:mm:ss"


def datetime_column(label: str) -> st.column_config.DatetimeColumn:
    return st.column_config.DatetimeColumn(label, format=DATETIME_FORMAT)


@st.cache_resource
def settings() -> DashboardSettings:
    return DashboardSettings.from_env()


@st.cache_resource
def _client(base_url: str) -> ApiClient:
    return ApiClient(base_url)


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def _cached(base_url: str, method: str, params: tuple[tuple[str, Any], ...]) -> Any:
    return getattr(_client(base_url), method)(**dict(params))


@st.cache_data(ttl=CATALOG_TTL_SECONDS, show_spinner=False)
def _cached_catalog(base_url: str) -> Any:
    return _client(base_url).catalog()


class CachedApi:
    """``ApiClient`` behind the cache, with the last good answer kept for outages."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.stale_since: datetime | None = None

    def __getattr__(self, method: str) -> Callable[..., Any]:
        def call(**params: Any) -> Any:
            fallback_key = (
                method,
                tuple(sorted((k, v) for k, v in params.items() if k not in ("since", "until"))),
            )
            last: dict = st.session_state.setdefault("_last_answers", {})
            try:
                if method == "catalog":
                    answer = _cached_catalog(self.base_url)
                else:
                    answer = _cached(self.base_url, method, tuple(sorted(params.items())))
            except ApiUnavailableError:
                if fallback_key not in last:
                    raise
                self.stale_since = self.stale_since or datetime.now(UTC)
                return last[fallback_key]
            last[fallback_key] = answer
            return answer

        return call


def api() -> Any:
    """The API for this session: a fake in tests, else the cached client."""
    if API_KEY not in st.session_state:
        st.session_state[API_KEY] = CachedApi(settings().api_url)
    return st.session_state[API_KEY]


# Sidebar filters --------------------------------------------------------------------------


@dataclass(frozen=True)
class Filters:
    run: dict[str, Any] | None  # None = every run
    window: str  # a key of WINDOWS
    domain: str | None  # None = every domain
    live: bool

    @property
    def run_id(self) -> str | None:
        return self.run["run_id"] if self.run else None

    def time_window(self, now: datetime | None = None) -> Window:
        step = settings().refresh_seconds or 5
        return make_window(
            WINDOWS[self.window], now=now or datetime.now(UTC), run=self.run, step_seconds=step
        )


def sidebar() -> Filters:
    """Run, time window, domain and live refresh; shared by every page."""
    source = api()
    with st.sidebar:
        try:
            runs = source.runs()
            known_domains = sorted({t["domain"] for t in source.catalog()["device_types"]})
        except ApiUnavailableError:
            st.error("API unavailable")
            runs, known_domains = [], []
        run_ids = [r["run_id"] for r in runs]  # newest first: the current run is the default
        recent = run_ids[0] if run_ids else None
        chosen = st.selectbox(
            "Run",
            [*run_ids, ALL],
            key="run",
            format_func=lambda r: f"Current ({r})" if r == recent else r,
            help="Every `stack up` starts a new run",
        )
        window = st.selectbox("Time window", list(WINDOWS), key="window")
        domain = st.selectbox("Domain", [ALL, *known_domains], key="domain")
        refresh = settings().refresh_seconds
        live = st.toggle(
            f"Live (every {refresh:g} s)", value=refresh > 0, key="live", disabled=refresh <= 0
        )
    run = next((r for r in runs if r["run_id"] == chosen), None)
    return Filters(run=run, window=window, domain=None if domain == ALL else domain, live=live)


def filters() -> Filters:
    return st.session_state["filters"]


def live(body: Callable[[], None]) -> None:
    """Run ``body`` as a fragment refreshed every few seconds while live is on."""
    every = settings().refresh_seconds if filters().live else None

    def guarded() -> None:
        try:
            body()
        except ApiUnavailableError as exc:
            unavailable(exc)
        source = api()
        if getattr(source, "stale_since", None) is not None:
            st.warning(
                f"API unavailable since {source.stale_since:%H:%M:%S} UTC: showing the "
                "last data received; retrying."
            )
            source.stale_since = None

    st.fragment(guarded, run_every=every or None)()


def unavailable(exc: Exception) -> None:
    retry = settings().refresh_seconds if filters().live else None
    when = f" Retrying every {retry:g} s." if retry else " Reload the page to retry."
    st.warning(f"API unavailable: {exc}.{when}")
