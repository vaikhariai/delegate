# syntax=docker/dockerfile:1
# Delegate: one container serving the API, the in-process worker and the built web app.
FROM node:22-slim AS web
WORKDIR /web
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY apps/web/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080 \
    DELEGATE_PGDATA=/tmp/delegate-pgdata DELEGATE_WEB_DIST=/srv/apps/web/dist DELEGATE_LOG_FORMAT=json
WORKDIR /srv
RUN pip install --no-cache-dir uv==0.13.6
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY packages/ ./packages/
COPY services/ ./services/
COPY migrations/ ./migrations/
COPY fixtures/ ./fixtures/
COPY evaluation/RESULTS.json* evaluation/scenarios.json evaluation/scenarios.sha256 evaluation/oracle.py evaluation/gate_harness.py ./evaluation/
RUN uv sync --frozen --no-dev --no-editable && rm -rf /root/.cache
COPY --from=web /web/dist ./apps/web/dist
RUN useradd --create-home --uid 10001 delegate && chown -R delegate /srv
USER delegate
EXPOSE 8080
CMD ["/srv/.venv/bin/uvicorn", "delegate.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-server-header", "--app-dir", "/srv/services/api"]
