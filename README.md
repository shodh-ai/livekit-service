# livekit-service/README.md
LiveKit-based real-time tutoring agent service (Rox Conductor). Provides both an HTTP server (FastAPI) for local/dev control and a LiveKit Agent worker that connects into rooms to orchestrate tutoring sessions.

This README summarizes how to run and operate the service and links to detailed docs in `docs/`.

## Contents

- Overview
- Architecture
- Quick start
- Environment configuration
- API (summary)
- Local development
- Deployment
- Security
- Troubleshooting
- Runbooks

For deep dives, see the docs/ directory:
- docs/architecture.md
- docs/api.md
- docs/environment.md
- docs/local-development.md
- docs/deployment.md
- docs/security.md
- docs/troubleshooting.md
- docs/runbooks.md

## Overview

The service runs a LiveKit Agent (the "Conductor") that:
- Joins a LiveKit room and manages real-time tutoring interactions.
- Intercepts student speech, forwards context to the Brain (LangGraph), and executes returned action scripts.
- Sends typed UI actions to the frontend viewer via LiveKit RPC.
- Sends browser automation commands to a browser pod via the LiveKit data channel.

It can also run as a small FastAPI server providing dev utilities and health checks.

## Architecture

Key files:
- `rox/main.py` — Conductor agent, FastAPI app, and CLI entry for LiveKit Agents.
- `rox/rpc_services.py` — LiveKit RPC handlers receiving frontend events.
- `rox/frontend_client.py` — Sends UI actions to the viewer via RPC.
- `rox/browser_pod_client.py` — Sends JSON commands to the browser pod over data channel.
- `rox/langgraph_client.py` — Calls the Brain (LangGraph) HTTP API.
- `rox/protos/interaction.proto` — Protobuf schema for RPC.
- `rox/generated/protos/*` — Generated protobuf classes.
- `worker.py` — Cloud Run Job style agent runner driven by env.
- `local_test_server.py` — Minimal dev worker server for local usage.

Read more: `docs/architecture.md`

## Quick start

1) Install and configure
- `python -m venv venv && source venv/bin/activate`
- `pip install -r requirements.txt`
- `cp sample.env .env` and set `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, `DEEPGRAM_API_KEY`

1a) Download required files
- `cd rox`
- `python main.py download-files`
- `cd ..`

2) Run local server (dev)
- Preferred: `python local_test_server.py` (starts FastAPI on port 5005)
- Health: `curl -s http://localhost:5005/ | jq`
- Alternative: `python -m uvicorn rox.main:app --host 0.0.0.0 --port 5005`

3) Spawn an agent (dev)
- POST `/local-spawn-agent` with room details (see `docs/api.md`)

4) Run as agent (CLI)
- `python rox/main.py connect --url wss://<livekit> --room <name> --api-key $LIVEKIT_API_KEY --api-secret $LIVEKIT_API_SECRET`

## Environment configuration

All variables are described in `docs/environment.md`.

Required for agent mode:
- `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, and a room name (CLI arg or env).

Common optional:
- `LANGGRAPH_TUTOR_URL`, `BROWSER_POD_HTTP_URL` (or `VNC_LISTENER_HTTP_URL`), `DEEPGRAM_API_KEY`.

## API (summary)

Base URL (server mode): `http://localhost:5005`

- `GET /` — health check
- `POST /run-agent` — spawn an agent subprocess (dev utility)
- `POST /dev/send-browser-command` — send a one-off browser command via data channel
- `POST /local-spawn-agent` — spawn an in-process agent (dev)

LiveKit RPC and data channel are used at runtime to interact with the viewer and browser pod. See `docs/api.md`.

## Local development

- Use FastAPI server for dev tools and testing endpoints.
- Use CLI agent mode to connect to rooms and observe runtime behavior.
- See `docs/local-development.md` for curl examples and workflows.

## Deployment

- Container image provided (Dockerfile). Defaults to FastAPI unless `LIVEKIT_URL` and `ROOM_NAME` are set, in which case it runs as agent.
- For Cloud Run Job, use `worker.py` and set env variables from Cloud Tasks payload.
- See `docs/deployment.md` for details.

## Security

- Protect dev endpoints from public exposure.
- Store secrets in a secret manager; do not log sensitive values.
- See `docs/security.md`.

## Troubleshooting and runbooks

- Common issues and fixes: `docs/troubleshooting.md`
- Operational playbooks: `docs/runbooks.md`

## Legacy notes

Previous quick steps for reference:
1. pip install -r requirements.txt
2. cd rox
3. set .env in rox folder
4. python main.py download-files
5. cd ..
6. python3 -m grpc_tools.protoc --python_out=rox/generated/protos --grpc_python_out=rox/generated/protos --proto_path=rox/protos rox/protos/interaction.proto
7. cd rox
8. python main.py

