# Local Development Guide

## Prereqs
- Python 3.11+ (Dockerfile uses 3.11-slim; PYTHON_VERSION.md mentions 3.9, but code and deps work with 3.11)
- A LiveKit server (local or hosted)
- Deepgram API key for STT
- Optional: LangGraph Student Tutor service running locally (HTTP)

## Setup
```bash
# From livekit-service/
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Download required files (run inside rox/)
cd rox
python main.py download-files
cd ..

# Copy sample env and fill values
cp sample.env .env
# Edit .env and set:
# LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET, DEEPGRAM_API_KEY
# Optionally LANGGRAPH_TUTOR_URL and BROWSER_POD_HTTP_URL
```

## Generate protobufs (if needed)
The repo includes generated files in `rox/generated/protos/`. If you update `rox/protos/interaction.proto`, regenerate:
```bash
python -m grpc_tools.protoc \
  --python_out=rox/generated/protos \
  --grpc_python_out=rox/generated/protos \
  --proto_path=rox/protos \
  rox/protos/interaction.proto
```

## Run local dev server
Preferred:
```bash
python local_test_server.py
```
- Health: `curl -s http://localhost:5005/ | jq`

Alternative (uvicorn FastAPI):
```bash
python -m uvicorn rox.main:app --host 0.0.0.0 --port 5005
```

Spawn agent in‑process (async):
```bash
curl -s -X POST http://localhost:5005/local-spawn-agent \
  -H 'Content-Type: application/json' \
  -d '{
    "room_name":"sess-local-1",
    "ws_url":"wss://<livekit-host>",
    "api_key":"...",
    "api_secret":"...",
    "agent_identity":"agent-sess-local-1",
    "student_token_metadata":"{}"
  }' | jq
```

## Send a dev browser command
```bash
curl -s -X POST http://localhost:5005/dev/send-browser-command \
  -H 'Content-Type: application/json' \
  -d '{
    "room_name":"sess-local-1",
    "tool_name":"browser_navigate",
    "parameters":{"url":"https://example.com"}
  }' | jq
```

## Run as agent (CLI mode)
```bash
python rox/main.py connect \
  --url wss://<livekit-host> \
  --room sess-local-1 \
  --api-key $LIVEKIT_API_KEY \
  --api-secret $LIVEKIT_API_SECRET
```
The CLI will call `entrypoint()` with a configured `JobContext`.

## Logs
- The Conductor logs extensively via `logging`. Look for lines from `rox.main`, `rox.rpc_services`, `rox.langgraph_client`, `rox.frontend_client`.
- LiveKit SDK logs connection status and RPC; Deepgram logs STT activity.

## Integration checks
- Frontend receives SUGGESTED_RESPONSES and other UI actions over RPC (see `rox/frontend_client.py`).
- Browser pod receives JSON commands over LiveKit data channel (see `rox/browser_pod_client.py`).
- LangGraph service receives POSTs from `rox/langgraph_client.py`.
