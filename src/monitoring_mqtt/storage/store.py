"""The storage facade used by the API: transactions, writes, queries, availability.

Database outages surface as ``StorageUnavailableError`` so the API can answer 503 and the
ingestion consumer retries later.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager

from sqlalchemy import Connection, Engine
from sqlalchemy.exc import DBAPIError, OperationalError

from monitoring_mqtt.core.alerts import AlertEvent
from monitoring_mqtt.core.models import Measurement
from monitoring_mqtt.storage import queries, writes
from monitoring_mqtt.storage.database import make_engine, ping
from monitoring_mqtt.storage.settings import DatabaseSettings
from monitoring_mqtt.storage.writes import Received, WriteResult


class StorageUnavailableError(RuntimeError):
    """The database cannot be reached right now."""


class Store:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @classmethod
    def from_settings(cls, settings: DatabaseSettings) -> Store:
        return cls(make_engine(settings))

    def close(self) -> None:
        self.engine.dispose()

    @contextmanager
    def _transaction(self) -> Iterator[Connection]:
        try:
            with self.engine.begin() as connection:
                yield connection
        except OperationalError as exc:
            raise StorageUnavailableError(str(exc).splitlines()[0]) from exc
        except DBAPIError as exc:
            if exc.connection_invalidated:
                raise StorageUnavailableError(str(exc).splitlines()[0]) from exc
            raise

    def _read[T](self, query: Callable[[Connection], T]) -> T:
        with self._transaction() as connection:
            return query(connection)

    def ping(self) -> None:
        try:
            ping(self.engine)
        except (OperationalError, DBAPIError) as exc:
            raise StorageUnavailableError(str(exc).splitlines()[0]) from exc

    def store_measurements(self, items: list[Received[Measurement]]) -> WriteResult:
        with self._transaction() as connection:
            return writes.store_measurements(connection, items)

    def store_alerts(self, items: list[Received[AlertEvent]]) -> WriteResult:
        with self._transaction() as connection:
            return writes.store_alerts(connection, items)

    # Read side (see queries.py for the parameters) ----------------------------------

    def list_devices(self, **filters) -> queries.Page:
        return self._read(lambda c: queries.list_devices(c, **filters))

    def get_device(self, device_id: str):
        return self._read(lambda c: queries.get_device(c, device_id))

    def list_measurements(self, **filters) -> queries.Page:
        return self._read(lambda c: queries.list_measurements(c, **filters))

    def series(self, bucket_seconds: float, **filters):
        return self._read(lambda c: queries.series(c, bucket_seconds, **filters))

    def list_alerts(self, **filters) -> queries.Page:
        return self._read(lambda c: queries.list_alerts(c, **filters))

    def get_alert(self, alert_id):
        return self._read(lambda c: queries.get_alert(c, alert_id))

    def list_runs(self, **filters):
        return self._read(lambda c: queries.list_runs(c, **filters))

    def get_run(self, run_id: str):
        return self._read(lambda c: queries.get_run(c, run_id))

    def stats(self):
        return self._read(queries.stats)
