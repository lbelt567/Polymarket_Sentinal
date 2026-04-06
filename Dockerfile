FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY scripts ./scripts
COPY config.yaml ./
COPY .env.example ./

RUN pip install --no-cache-dir .

RUN mkdir -p /app/data/alerts /app/data/archive

HEALTHCHECK --interval=60s --timeout=10s --start-period=180s --retries=3 \
  CMD python scripts/healthcheck.py --sqlite-path /app/data/sentinel.db --max-tick-age-sec 300 --max-gatekeeper-age-sec 900 --min-active-markets 25

CMD ["python", "-m", "sentinel"]
