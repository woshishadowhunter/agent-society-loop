FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --gid 10001 seed-society \
    && useradd --uid 10001 --gid 10001 --create-home seed-society \
    && mkdir --parents /data \
    && chown 10001:10001 /data

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m pip install ".[postgres]"

USER 10001:10001

VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD ["seed-society", "health", "--db", "/data/seed-society.db", "--json"]

ENTRYPOINT ["seed-society"]
CMD ["health", "--db", "/data/seed-society.db", "--json"]
