"""API entrypoint: ``python -m monitoring_mqtt.api`` (the ``api`` service).

Serves on ``API_PORT`` (default 8000) on every interface of the container; Compose
publishes it on the host's loopback by default. The database comes from ``DATABASE_URL``.
"""

from __future__ import annotations

import os
import sys

import uvicorn

from monitoring_mqtt.core.config import ConfigError, env_int, log_level_from_env
from monitoring_mqtt.core.logging import configure_logging


def main() -> int:
    try:
        level = log_level_from_env()
        port = env_int(os.environ, "API_PORT", 8000, 1, 65535)
    except ConfigError as exc:
        print(f"invalid configuration: {exc}", file=sys.stderr)
        return 2
    configure_logging(level)
    uvicorn.run(
        "monitoring_mqtt.api.app:create_app",
        factory=True,
        host="0.0.0.0",  # inside the container; the host side is bound by Compose
        port=port,
        log_config=None,  # keep the project's UTC key=value logging
        access_log=level == "DEBUG",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
