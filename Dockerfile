FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY CONTRACT.md ./
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH" FC_DB=/data/flightcontrol.sqlite3
VOLUME /data
CMD ["flightcontrol", "web", "--host", "0.0.0.0", "--port", "8000"]
