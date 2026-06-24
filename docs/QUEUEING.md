# Workflow Queueing

The Agent platform supports two workflow dispatch modes:

- `db`: local/default mode. Workers poll the `workflow_jobs` table and claim jobs with database locks.
- `redis`: production dispatch mode. The database remains the source of truth, and Redis carries job-id signals so multiple workers can wake up quickly and scale horizontally.

## Configuration

Local development:

```env
AGENT_QUEUE_BACKEND=db
```

Production-style worker dispatch:

```env
AGENT_QUEUE_BACKEND=redis
AGENT_REDIS_URL=redis://agent-redis:6379/0
AGENT_REDIS_QUEUE_NAME=agent:workflow_jobs
AGENT_REDIS_BLOCK_TIMEOUT_SECONDS=5
```

When a job is created or retried, the platform:

1. writes the durable job record to `workflow_jobs`
2. pushes the job id to Redis
3. lets workers pop that job id and claim the matching DB row atomically

If Redis contains a duplicate or stale signal, the worker discards it and falls back to the DB queue scan. If Redis is temporarily unavailable, queued jobs remain in the database.

## Worker Startup

```powershell
.\.venv\Scripts\python.exe scripts\worker.py --worker-id worker-1
```

In Docker production compose, `agent-redis` is included and `workflow-worker` uses the Redis queue by default.

## Verification

```powershell
.\.venv\Scripts\python.exe scripts\queue_smoke_test.py
.\.venv\Scripts\python.exe scripts\preflight.py
```

`scripts\preflight.py` prints:

- `queue_backend`
- `queue_enabled`
- `queue_redis_url_configured`
- `queue_redis_dependency_available`
- `queue_reachable`
- `queue_error`

## Operational Notes

- Keep the database job table as the authoritative state machine.
- Run more than one worker process for horizontal capacity.
- Monitor queue lag through Redis list length and DB `workflow_jobs` counts.
- Keep job processing idempotent because external queues can deliver duplicate signals.
