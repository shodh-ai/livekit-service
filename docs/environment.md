# Environment Configuration

This service reads configuration from environment variables and `.env` (loaded by `dotenv` in `rox/main.py`). Below is a comprehensive list grouped by responsibility.

## LiveKit connection (required for agent mode)
- LIVEKIT_URL — WebSocket URL to your LiveKit server (e.g., `wss://livekit.example.com`).
- LIVEKIT_API_KEY — API key for minting/machine access.
- LIVEKIT_API_SECRET — API secret.
- LIVEKIT_ROOM_NAME — Room name used by `worker.py` and dev utilities.
- ROOM_NAME — Used by the Dockerfile’s CMD path to decide agent vs server mode (container only). Set alongside `LIVEKIT_URL` to run the image as an agent.

Recommendations:
- In containerized deployments, set both `LIVEKIT_ROOM_NAME` and `ROOM_NAME` to the same value to be compatible with all code paths.

## Brain (LangGraph) service
- LANGGRAPH_TUTOR_URL — Base URL of the Student Tutor service (preferred).
- LANGGRAPH_API_URL — Legacy fallback if `LANGGRAPH_TUTOR_URL` is not set.

## Audio / Speech
- DEEPGRAM_API_KEY — Required to enable Deepgram STT in the agent.
- (Optional) Other LLM/TTS keys if you extend the agent (e.g., OPENAI_API_KEY for future features).

## Visual context (optional)
- BROWSER_POD_HTTP_URL — HTTP endpoint of the browser pod that serves `/screenshot` returning `{ screenshot_b64, url, timestamp }`.
- VNC_LISTENER_HTTP_URL — Legacy fallback to VNC listener HTTP endpoint with the same `/screenshot` contract.

When set, screenshots may be attached to LangGraph requests as `visual_context`.

## Browser join timing knobs (optional)
- BROWSER_JOIN_WAIT_SEC — Seconds to wait for the browser participant to appear on session start before skipping initial navigation. Default `6`.
- BROWSER_JOIN_WATCH_SEC — If not present initially, max seconds to keep watching for a late-joining browser participant to send the initial navigate once. Default `60`.

## Local/dev utilities
- PORT — FastAPI port when using `uvicorn rox.main:app` (commonly `5005`). The Dockerfile default is `8080` for Cloud Run.
- PYTHONPATH — Set automatically in Docker to `/app`.

## Example .env
See `sample.env` and extend as needed:

```
LIVEKIT_URL=wss://livekit.example.com
LIVEKIT_API_KEY=lk_...
LIVEKIT_API_SECRET=secret_...
DEEPGRAM_API_KEY=dg_...
LANGGRAPH_TUTOR_URL=http://localhost:8001
BROWSER_POD_HTTP_URL=http://browser-pod:8777
# For container CMD agent mode
ROOM_NAME=sess-abc123
```

## Validation
- The agent path requires `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, and a room name (CLI arg or env depending on mode).
- LangGraph is optional for startup but required for full tutoring flows; otherwise actions may be empty.
- Without `BROWSER_POD_HTTP_URL`/`VNC_LISTENER_HTTP_URL`, `visual_context` is omitted.
