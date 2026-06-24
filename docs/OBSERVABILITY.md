# Observability

The Agent platform exposes three production observability layers:

- request and workflow correlation through W3C `traceparent`
- Prometheus-compatible metrics at `/metrics`
- structured application logs and optional OpenTelemetry trace export

## Structured Logs

For production, enable JSON logs:

```env
AGENT_LOG_LEVEL=INFO
AGENT_LOG_FORMAT=json
AGENT_SERVICE_NAME=enterprise-workflow-agent
AGENT_SERVICE_VERSION=2026.06.22
```

HTTP logs include:

- `request_id`
- `trace_id`
- `span_id`
- `http_method`
- `http_path`
- `http_status`
- `duration_ms`

Workflow and worker logs include fields such as:

- `run_id`
- `job_id`
- `tenant_id`
- `worker_id`
- `node_name`
- `tool_name`
- `error_type`

These JSON logs can be shipped by your platform log agent to ELK/OpenSearch, Loki, Datadog, CloudWatch, Azure Monitor, or another log backend.

## Metrics

Enable metrics:

```env
AGENT_METRICS_ENABLED=true
AGENT_METRICS_AUTH_REQUIRED=true
```

Endpoint:

```text
GET /metrics
```

The endpoint includes workflow, job, approval, ticket, email, outbox, latency, and login-lockout metrics.

## OpenTelemetry

To export traces through OTLP HTTP:

```env
AGENT_OTEL_ENABLED=true
AGENT_OTEL_SERVICE_NAME=enterprise-workflow-agent
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318/v1/traces
OTEL_EXPORTER_OTLP_HEADERS=
```

For local debugging without a collector:

```env
AGENT_OTEL_ENABLED=true
AGENT_OTEL_EXPORT_CONSOLE=true
```

When OpenTelemetry is enabled, the platform emits spans for HTTP requests, workflow job processing, workflow runs, and workflow steps.

## Verification

```powershell
.\.venv\Scripts\python.exe scripts\preflight.py
Invoke-RestMethod http://127.0.0.1:8010/api/readiness
Invoke-RestMethod http://127.0.0.1:8010/api/health
Invoke-RestMethod http://127.0.0.1:8010/metrics
```

`scripts\preflight.py` prints `log_format`, `log_level`, `otel_enabled`, and `otel_configured`.
