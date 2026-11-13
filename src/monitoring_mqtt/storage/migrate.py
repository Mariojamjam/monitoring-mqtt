"""Schema migrations: ``python -m monitoring_mqtt.storage.migrate [upgrade|current|sql]``.

Runs as the one-off ``migrate`` service on every ``stack up``, before the API starts.
Alembic is configured in code (no alembic.ini), with the migration scripts inside the
package so they ship in the application image.

    upgrade   apply every pending migration (default)
    current   print the applied revision
    sql       print the SQL of every migration without a database (offline mode)
"""

from __future__ import annotations

import logging
import sys
import time

from alembic import command
from alembic.config import Config
from sqlalchemy.exc import OperationalError

from monitoring_mqtt.core.logging import configure_logging
from monitoring_mqtt.storage.database import make_engine, ping
from monitoring_mqtt.storage.settings import DatabaseSettings

logger = logging.getLogger("monitoring_mqtt.migrate")

SCRIPT_LOCATION = "monitoring_mqtt.storage:migrations"
WAIT_SECONDS = 30.0  # the database may still be starting


def alembic_config(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", SCRIPT_LOCATION)
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def wait_for_database(settings: DatabaseSettings, timeout: float = WAIT_SECONDS) -> None:
    engine = make_engine(settings)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                ping(engine)
                return
            except OperationalError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(1)
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    action = args[0] if args else "upgrade"
    configure_logging("INFO")
    settings = DatabaseSettings.from_env()
    config = alembic_config(settings.url)
    if action == "sql":
        command.upgrade(config, "head", sql=True)
        return 0
    try:
        wait_for_database(settings)
    except OperationalError as exc:
        logger.error("database unreachable: %s", str(exc).splitlines()[0])
        return 1
    if action == "upgrade":
        command.upgrade(config, "head")
        logger.info("database schema up to date")
    elif action == "current":
        command.current(config)
    else:
        logger.error("unknown action %r: use upgrade, current or sql", action)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
