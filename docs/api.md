# API Reference

The service exposes a small HTTP API (FastAPI in `rox/main.py`) for development and control, and communicates with the frontend over LiveKit RPC/data channels at runtime.

Base URL (server mode): `http://localhost:5005`

## HTTP Endpoints (FastAPI)

- GET `/`
  - Health. Returns: `{ "service": "Rox Agent Service", "status": "running" }`

- POST `/run-agent`
  - Spawns an agent as a subprocess of the current FastAPI process, connecting to the specified LiveKit room.
  - Body (JSON):
    ```json
    { "room_name": "sess-...", "room_url": "wss://<livekit-host>" }
    ```
  - Uses `LIVEKIT_API_KEY` and `LIVEKIT_API_SECRET` from environment.

- POST `/dev/send-browser-command`
  - Dev-only utility to send a single browser command to the pod participant via LiveKit data channel.
  - Body (JSON):
    ```json
    {
      "room_name": "sess-...",
      "tool_name": "browser_navigate|browser_click|browser_type|...",
      "parameters": {"url": "https://example.com"},
      "room_url": "wss://<livekit-host>",       // optional
      "token": "<jwt>",                       // optional; if omitted, server will mint a short-lived token
      "participant_identity": "cmd-relay-..."  // optional
    }
    ```
  - Returns: `{ ok, room, tool, parameters }`

- POST `/local-spawn-agent`
  - Dev worker endpoint to spawn an agent in the current process asynchronously (mirrors `local_test_server.py`).
  - Body (JSON):
    ```json
    {
      "room_name": "sess-...",
      "ws_url": "wss://<livekit-host>",
      "api_key": "...",
      "api_secret": "...",
      "agent_identity": "agent-sess-...",
      "student_token_metadata": "{...}"
    }
    ```
  - Returns: `{ status: "accepted", message }`

## Agent CLI (LiveKit Agents)

When invoked in agent mode, the LiveKit Agents CLI handles arguments and calls `entrypoint()` from `rox/main.py`:

```bash
python rox/main.py connect --url wss://<livekit> --room <name> --api-key <key> --api-secret <secret>
```

## LiveKit RPC to Frontend

The agent uses LiveKit RPC to send protobuf messages to the viewer participant (frontend). The method is:
- `rox.interaction.ClientSideUI/PerformUIAction` (see `rox/frontend_client.py`)

Message type for requests is `AgentToClientUIActionRequest` defined in `rox/protos/interaction.proto`. The helper `utils/ui_action_factory.build_ui_action_request()` maps high-level action names and parameters into typed protobuf payloads.

Common action types include:
- `SUGGESTED_RESPONSES` — payload contains `suggestions` (rich) or `responses` (legacy strings) and an optional `title`.
- `DRAW_ACTION` and Excalidraw actions — for canvas operations (clear, update, modify, highlight, etc.).
- `BROWSER_NAVIGATE`, `BROWSER_CLICK`, `BROWSER_TYPE` — UI hints for the viewer; actual browser automation is sent to the browser pod via data channel.
- `SETUP_JUPYTER`, `JUPYTER_*` actions — for Jupyter interactions in the learning flow.

Example (pseudocode):
```python
from rox.frontend_client import FrontendClient
await FrontendClient().set_ui_state(room, viewer_identity, {"mode": "drawing", "tool": "pen"})
```

## Data Channel to Browser Pod

The agent uses the LiveKit data channel to send JSON commands to the browser pod participant (identity `browser-bot-<room|session_id>`):
- See `rox/browser_pod_client.py` → `send_browser_command(room, browser_identity, tool_name, parameters)`.
- Payload:
  ```json
  { "tool_name": "browser_navigate", "parameters": {"url":"https://example.com"}, "target":"browser-bot-sess-...", "source":"agent" }
  ```

## Brain (LangGraph) API

The agent calls a separate LangGraph service via HTTP (see `rox/langgraph_client.py`). Endpoints:
- `POST /start_session` — initial turn.
- `POST /handle_interruption` — interruption turns.
- `POST /handle_response` — regular turns.

Body fields include `session_id`, `student_id`, `curriculum_id`, optional `current_lo_id`, `student_input`, and optional `visual_context` (with `screenshot_b64`).
