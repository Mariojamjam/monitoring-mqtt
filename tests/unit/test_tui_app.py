import asyncio

from rich.console import Console
from textual.widgets import DataTable, TabbedContent
from tui_messages import E0, claim, status, telemetry

from monitoring_mqtt.tui import render
from monitoring_mqtt.tui.app import DetailScreen, MonitoringApp
from monitoring_mqtt.tui.state import Connection, Message, SystemState

POULTRY = "monitoring/v1/poultry/#"


class FakeFeed:
    client_id = "tui-test"

    def __init__(self, items) -> None:
        self.items = list(items)
        self.started = self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def drain(self):
        items, self.items = self.items, []
        return items


def scenario():
    return [
        Connection(True, E0, "broker:1883"),
        Message("$SYS/broker/version", b"mosquitto version 2.1.2", E0),
        claim("aaaaaa", node="pi"),
        claim("bbbbbb", node="pc-1"),
        telemetry("poultry-aaaaaa-001", 1, E0 + 1),
        telemetry("cattle-bbbbbb-001", 1, E0 + 1, domain="cattle"),
        status("monitor-poultry-000001", POULTRY, E0 + 1, received=1, poultry=1),
        Message("monitoring/v1/poultry/ammonia/bad-1", b"garbage", E0 + 1),
    ]


def make_app():
    feed = FakeFeed(scenario())
    app = MonitoringApp(feed, SystemState(5.0, feed.client_id), "broker:1883",
                        clock=lambda: E0 + 2)  # fmt: skip
    return app, feed


def run(coro) -> None:
    asyncio.run(coro)


def test_overview_shows_publishers_broker_and_subscribers() -> None:
    async def scenario_test() -> None:
        app, feed = make_app()
        async with app.run_test(size=(180, 50)) as pilot:
            await pilot.pause()
            assert feed.started
            screen = app.export_screenshot()
            for text in ("poultry-aaaaaa-001", "monitor-poultry-000001", "pi", "pc-1",
                         "no subscriber", "Mosquitto 2.1.2", "invalid 1"):  # fmt: skip
                assert text in screen.replace("&#160;", " "), text
        assert feed.stopped

    run(scenario_test())


def test_tabs_tables_and_detail() -> None:
    async def scenario_test() -> None:
        app, _ = make_app()
        async with app.run_test(size=(180, 50)) as pilot:
            await pilot.press("2")
            assert app.query_one(TabbedContent).active == "devices"
            table = app.query_one("#device-table", DataTable)
            await pilot.pause()
            assert table.row_count == 2
            table.focus()
            await pilot.press("enter")
            assert isinstance(app.screen, DetailScreen)
            await pilot.press("escape")
            assert not isinstance(app.screen, DetailScreen)

            await pilot.press("3")
            await pilot.pause()
            assert app.query_one("#monitor-table", DataTable).row_count == 1

            assert "Selected monitor" in app.export_screenshot().replace("&#160;", " ")

            await pilot.press("4")
            await pilot.pause()
            events = app.query_one("#event-table", DataTable)
            assert events.row_count == 5  # system changes: broker, 2 workers, monitor, invalid
            await pilot.press("t")  # include telemetry
            await pilot.pause()
            assert events.row_count == 7

    run(scenario_test())


def test_device_filter() -> None:
    async def scenario_test() -> None:
        app, _ = make_app()
        async with app.run_test(size=(180, 50)) as pilot:
            await pilot.press("2", "slash")
            await pilot.press(*"cattle")
            await pilot.pause()
            assert app.query_one("#device-table", DataTable).row_count == 1

    run(scenario_test())


def test_monitor_detail_shows_subscription_and_received() -> None:
    state = SystemState(5.0)
    state.apply_all(scenario())
    monitor = state.monitors["monitor-poultry-000001"]
    console = Console(width=160, record=True)
    console.print(render.monitor_detail(state, monitor, E0 + 2))
    output = console.export_text()
    assert "subscribes  monitoring/v1/poultry/#" in output
    assert "nothing received yet" in output  # the synthetic status carries no messages


def test_sparkline() -> None:
    assert render.sparkline([1, 2, 3]) == "▁▄█"
    assert render.sparkline([5, 5]) == "▁▁"
    assert render.sparkline([]) == ""


def test_devices_show_simulated_mode_and_out_of_range_values() -> None:
    from monitoring_mqtt.core.simulation import EpisodeKind, EpisodePhase, EpisodeSource, SimEvent
    from monitoring_mqtt.tui.state import Band

    event = SimEvent(
        run_id="lab-01", worker_id="aaaaaa", device_id="poultry-aaaaaa-001",
        kind=EpisodeKind.ANOMALY, phase=EpisodePhase.START, source=EpisodeSource.MANUAL,
        direction="high",
    )  # fmt: skip
    items = [*scenario(), telemetry("poultry-aaaaaa-001", 2, E0 + 2, value=40.0),
             Message(event.topic, event.to_payload(), E0 + 2)]  # fmt: skip
    feed = FakeFeed(items)
    state = SystemState(5.0, feed.client_id, {"poultry_type": Band(0.0, 25.0)})
    app = MonitoringApp(feed, state, "broker:1883", clock=lambda: E0 + 2)

    async def scenario_test() -> None:
        async with app.run_test(size=(200, 50)) as pilot:
            await pilot.press("2")
            await pilot.pause()
            table = app.query_one("#device-table", DataTable)
            row = table.get_row("poultry-aaaaaa-001")
            mode = row[table.get_column_index("Mode")]
            value = row[table.get_column_index("Value")]
            assert mode.plain == "anomaly ↑"
            assert "red" in str(value.style)
            cattle = table.get_row("cattle-bbbbbb-001")
            assert cattle[table.get_column_index("Mode")].plain == "normal"

    run(scenario_test())
