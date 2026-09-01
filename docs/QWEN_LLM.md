# Qwen LLM Adapter

The Agent can call Alibaba Cloud Model Studio / Qwen through its OpenAI-compatible Chat Completions API.

Qwen is used in two safe places:

- Intent planning: classify the user's request and suggest category, priority, amount, recipient, and owner.
- Final-answer formatting: turn the backend result into concise Chinese Markdown with headings and bullet points.

Qwen does not directly execute external actions. Ticket creation, email sending, approvals, audit logging, tenant isolation, and permission checks remain enforced by backend code.

The same adapter also supports a self-hosted vLLM OpenAI-compatible endpoint. vLLM does not require an API key unless the endpoint is protected by a gateway.

## Configuration

Set these variables in `.env`, `.env.hr-demo`, or your cloud secret manager:

```text
AGENT_LLM_ENABLED=true
AGENT_LLM_PROVIDER=qwen
AGENT_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
AGENT_LLM_API_KEY=your-dashscope-api-key
# DASHSCOPE_API_KEY=your-dashscope-api-key
AGENT_LLM_MODEL=qwen-plus
AGENT_LLM_TIMEOUT_SECONDS=30
AGENT_LLM_TEMPERATURE=0.2
AGENT_LLM_MAX_TOKENS=900
AGENT_LLM_PLANNER_ENABLED=true
AGENT_LLM_FINAL_ANSWER_ENABLED=true
AGENT_LLM_MULTI_AGENT_REASONING_ENABLED=true
AGENT_LLM_PLANNER_MIN_CONFIDENCE=0.55
```

`AGENT_LLM_API_KEY` is preferred. `DASHSCOPE_API_KEY` is also supported as a fallback.

Do not put real API keys in frontend code or commit them to Git.

For an existing vLLM endpoint:

```text
AGENT_LLM_ENABLED=true
AGENT_LLM_PROVIDER=vllm
AGENT_LLM_BASE_URL=http://your-vllm-host:8000/v1
AGENT_LLM_API_KEY=
AGENT_LLM_MODEL=Qwen/Qwen2.5-32B-Instruct-AWQ
AGENT_LLM_PLANNER_ENABLED=true
AGENT_LLM_FINAL_ANSWER_ENABLED=true
AGENT_LLM_MULTI_AGENT_REASONING_ENABLED=true
```

To start the repository's NVIDIA GPU profile and connect both Agent and RAG inference to it:

```powershell
docker compose `
  -f docker-compose.prod.yml `
  -f docker-compose.vllm.yml `
  --profile gpu up --build -d
```

Tune `VLLM_TENSOR_PARALLEL_SIZE`, `VLLM_GPU_MEMORY_UTILIZATION`, and `VLLM_MAX_MODEL_LEN` for the actual GPU server. The 32B AWQ profile cannot be validated on a machine without a suitable NVIDIA GPU and driver stack.

## Local Smoke Test

```powershell
.\.venv\Scripts\python.exe scripts\llm_smoke_test.py --require-call
```

Without `--require-call`, the script prints the current LLM status and exits successfully when the key is not configured.

## Docker

For the production-style stack:

```powershell
docker compose --env-file .env.hr-demo -f docker-compose.prod.yml up -d --build workflow-agent workflow-worker
```

For the simple local stack:

```powershell
docker compose --env-file .env up -d --build
```

## Status Endpoints

```text
GET /api/health
GET /api/readiness
GET /api/admin/preflight
```

These responses include `llm.enabled`, `llm.provider`, `llm.model`, and whether the API key is configured.
