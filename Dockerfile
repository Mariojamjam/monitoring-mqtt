# Application image shared by every Python service (device workers, monitors, tools).
# The service to run is chosen by the Compose `command`.
FROM python:3.12.14-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencies first, from the lockfile, so code changes do not reinstall them.
COPY pyproject.toml uv.lock .python-version README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# Runtime configuration (device catalog) baked into the image; no bind mounts needed.
COPY config ./config
ENV DEVICES_CONFIG=/app/config/devices.yaml

RUN useradd --system --uid 10001 --no-create-home app
USER app
