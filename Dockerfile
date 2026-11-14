# Two images from one file:
#   app        (default) every Python service: device workers, monitors, consumers, API,
#              tools. Kept small: it also runs on Raspberry Pis.
#   dashboard  the Streamlit dashboard: the same package plus Streamlit, pandas and Altair.
# The service to run is chosen by the Compose `command`.
FROM python:3.12.14-slim-bookworm AS base

COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY pyproject.toml uv.lock .python-version README.md ./
RUN useradd --system --uid 10001 --no-create-home app


FROM base AS dashboard
# Dependencies first, from the lockfile, so code changes do not reinstall them. uv's
# download cache stays in a build cache mount, out of the image.
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-default-groups --group dashboard --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-default-groups --group dashboard --no-editable
# Streamlit keeps its machine id and settings in $HOME; give it a writable, empty one.
ENV HOME=/tmp
USER app


FROM base AS app
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-default-groups --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-default-groups --no-editable

# Runtime configuration (device catalog, alert rules) baked into the image; no bind mounts.
COPY config ./config
ENV DEVICES_CONFIG=/app/config/devices.yaml

USER app
