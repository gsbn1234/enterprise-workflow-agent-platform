ARG NODE_IMAGE=node:22-slim
ARG BASE_IMAGE=python:3.11-slim
FROM ${NODE_IMAGE} AS frontend-builder

WORKDIR /build
COPY package*.json ./
RUN npm ci
COPY vite.config.js ./
COPY frontend ./frontend
COPY app/static ./app/static
RUN npm run build

FROM ${BASE_IMAGE}

ARG SKIP_PIP_INSTALL=false

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN if [ "$SKIP_PIP_INSTALL" != "true" ]; then pip install --no-cache-dir -r requirements.txt; fi

COPY . .
COPY --from=frontend-builder /build/app/static/react ./app/static/react
EXPOSE 8010

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import json, urllib.request; data=json.load(urllib.request.urlopen('http://127.0.0.1:8010/api/readiness', timeout=3)); assert data['status'] == 'ok'; assert data['database']['status'] == 'ok'"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8010"]
