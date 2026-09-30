# The hosted web app. Deploys as-is to Fly.io, Render or Railway.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    FDM_CACHE_DIR=/data \
    FORWARDED_ALLOW_IPS=*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[web,advanced]" \
    && useradd --create-home app && mkdir -p /data && chown app /data

USER app
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz')"
CMD ["start-who-web"]
