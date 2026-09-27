FROM ghcr.io/astral-sh/uv:0.8.22 AS uv
FROM python:3.12-slim
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY salesforce-mapping.json salesforce-mapping-serpapi.json ./
RUN uv sync --frozen --no-dev && useradd --uid 10001 --create-home app
USER 10001
EXPOSE 8000
CMD ["/app/.venv/bin/uvicorn", "lead_api.api:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
