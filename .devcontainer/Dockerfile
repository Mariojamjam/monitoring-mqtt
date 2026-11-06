# Development image: Python 3.12, git and a non-root `vscode` user (from the base image),
# plus uv. The Docker CLI and Compose V2 come from the docker-outside-of-docker feature.
FROM mcr.microsoft.com/devcontainers/python:1-3.12-bookworm

# The base image ships a Yarn apt source whose signing key expired, which breaks
# `apt-get update` (used by the Docker feature). Yarn is not needed here.
RUN rm -f /etc/apt/sources.list.d/yarn.list

COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /uvx /usr/local/bin/

# Pre-create directories that become named volumes so they are owned by `vscode`.
RUN install -d -o vscode -g vscode /home/vscode/.cache/uv /home/vscode/.venvs
