# Deployment Guide

You can deploy this service in two primary modes:

- HTTP service (FastAPI) — useful for dev tools (`/local-spawn-agent`, `/dev/send-browser-command`) and for exposing a health endpoint. Suitable for Cloud Run Service or Kubernetes Deployment.
- Agent worker — connects to a specific LiveKit room and runs the Conductor. Suitable for Cloud Run Job (triggered by Cloud Tasks) or a long‑running container job.

## Container image
Build locally:
```bash
docker build -t livekit-service:latest .
```

## Cloud Run Service (HTTP mode)
- Container listens on port 8080 (Dockerfile).
- By default (per Dockerfile), if `LIVEKIT_URL` and `ROOM_NAME` are not set, the CMD runs FastAPI: `uvicorn rox.main:app --host 0.0.0.0 --port 8080`.
- Configure environment:
  - LIVEKIT_API_KEY, LIVEKIT_API_SECRET
  - LANGGRAPH_TUTOR_URL (optional)
  - DEEPGRAM_API_KEY
- Expose `/` health and `/local-spawn-agent` as needed (consider private ingress / IAM for dev‑only endpoints).

## Cloud Run Job (Agent mode)
When used as an agent runner triggered by Cloud Tasks (from the token service), you have two patterns:

1) Use `worker.py`
- Cloud Task (HTTP) invokes a Cloud Run Job passing env vars:
  - LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET, LIVEKIT_ROOM_NAME
  - AGENT_IDENTITY, STUDENT_TOKEN_METADATA (optional)
  - DEEPGRAM_API_KEY, LANGGRAPH_TUTOR_URL
- The job entrypoint is `python worker.py`, which:
  - Validates env, configures `WorkerOptions(entrypoint_fnc=entrypoint)`, and runs the agent loop.

2) Use the Dockerfile CMD agent path
- If `LIVEKIT_URL` and `ROOM_NAME` are set, container CMD runs:
  ```
  python rox/main.py connect --url "$LIVEKIT_URL" --room "$ROOM_NAME" \
    --api-key "$LIVEKIT_API_KEY" --api-secret "$LIVEKIT_API_SECRET"
  ```
- Ensure `ROOM_NAME` matches `LIVEKIT_ROOM_NAME` for consistency.

## Kubernetes
- For HTTP server: deploy a Deployment + Service on port 8080.
- For agent worker: deploy a Job or a CronJob template. Inject env via Secrets and ConfigMaps.

## Env summary (see docs/environment.md)
- Required for agent mode: LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET, and a room name (via CLI or env).
- Optional: LANGGRAPH_TUTOR_URL, BROWSER_POD_HTTP_URL, DEEPGRAM_API_KEY.

## Health and readiness
- HTTP: `GET /`
- Agent: use logs and LiveKit server metrics to confirm participant presence and data‑channel activity.

## Observability
- Logs to stdout with structured messages. Filter by module names.
- Consider integrating with GCP Cloud Logging or similar.

## Security considerations
- Lock down dev endpoints (`/local-spawn-agent`, `/dev/send-browser-command`).
- Store secrets in a secret manager; do not bake into images.
