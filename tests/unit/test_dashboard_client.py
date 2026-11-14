import httpx
import pytest

from monitoring_mqtt.dashboard.client import ApiClient, ApiError, ApiUnavailableError
from monitoring_mqtt.dashboard.settings import DashboardSettings


def client_answering(handler) -> ApiClient:
    http = httpx.Client(base_url="http://api:8000", transport=httpx.MockTransport(handler))
    return ApiClient("http://api:8000", client=http)


def test_lists_follow_the_cursor_up_to_a_limit() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        page = int(request.url.params.get("cursor") or 0)
        size = min(2, int(request.url.params["limit"]))  # pages of 2 at most
        items = [{"n": page * 2 + i} for i in range(size)]
        return httpx.Response(200, json={"items": items, "next_cursor": str(page + 1)})

    items = client_answering(handler).measurements(max_items=5, device_id="d1")
    assert [i["n"] for i in items] == [0, 1, 2, 3, 4]
    assert [r.url.params["limit"] for r in requests] == ["5", "3", "1"]  # never more
    assert all(r.url.params["device_id"] == "d1" for r in requests)


def test_last_page_stops_and_time_filters_use_the_api_names() -> None:
    from datetime import UTC, datetime

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"items": [{"n": 1}], "next_cursor": None})

    since = datetime(2026, 11, 14, 12, tzinfo=UTC)
    assert len(client_answering(handler).alerts(since=since, state=None)) == 1
    params = seen[0].url.params
    assert params["from"] == since.isoformat() and "state" not in params and len(seen) == 1


@pytest.mark.parametrize(
    "answer", [httpx.Response(503), httpx.ConnectError("refused"), httpx.ReadTimeout("slow")]
)
def test_outages_become_unavailable(answer) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(answer, Exception):
            raise answer
        return answer

    api = client_answering(handler)
    with pytest.raises(ApiUnavailableError):
        api.stats()
    assert api.ready() is False


def test_missing_and_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404 if "runs" in request.url.path else 422, text="bad")

    api = client_answering(handler)
    assert api.run("nope") is None
    with pytest.raises(ApiError):
        api.series(bucket="1")


def test_settings_from_env() -> None:
    assert DashboardSettings.from_env({}) == DashboardSettings("http://127.0.0.1:8000", 5.0)
    settings = DashboardSettings.from_env(
        {"API_URL": "http://api:8000/", "DASHBOARD_REFRESH_SECONDS": "0"}
    )
    assert settings == DashboardSettings("http://api:8000", 0.0)
    for bad in ("x", "-1"):
        with pytest.raises(ValueError, match="DASHBOARD_REFRESH_SECONDS"):
            DashboardSettings.from_env({"DASHBOARD_REFRESH_SECONDS": bad})
