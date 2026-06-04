FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
RUN uv sync --no-dev

EXPOSE 8000
CMD ["uv", "run", "uvicorn", "brain.main:app", "--host", "0.0.0.0", "--port", "8000"]
