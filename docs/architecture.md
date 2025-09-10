# LiveKit Service — Architecture

This service hosts Rox, a real‑time tutoring agent built on LiveKit Agents. It can run as:

- A FastAPI HTTP server for local/dev control and utilities.
- A LiveKit Agent worker that connects into a given room and orchestrates real‑time tutoring.

Core responsibilities:
- Join a LiveKit room and drive the session (audio, data channel, UI RPC) as the "Conductor".
- Bridge student speech and UI events to the Brain (LangGraph), and execute returned action scripts.
- Send UI actions and browser automation commands to the frontend viewer and a browser pod via LiveKit RPC/data channel.

## Code layout

- `rox/main.py`
  - Defines `RoxAgent` (the Conductor) subclassing `livekit.agents.Agent`.
  - Provides `async def entrypoint(ctx: JobContext)` used by the LiveKit Agent worker.
  - Exposes a FastAPI app with:
    - `GET /` — health
    - `POST /run-agent` — spawn an agent process via subprocess (dev utility)
    - `POST /dev/send-browser-command` — send a one‑off browser command through the data channel
    - `POST /local-spawn-agent` — dev worker endpoint to spawn an agent inside this process
  - CLI integration: runs `agents.cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))` in agent mode.

- `rox/rpc_services.py`
  - Implements RPC handlers invoked by the frontend over LiveKit RPC:
    - `student_wants_to_interrupt`, `student_mic_button_interrupt`
    - `student_spoke_or_acted` (rich input handler)
    - `InvokeAgentTask` (generic task queueing)
    - `TestPing` (connectivity + session start trigger)
  - RPCs enqueue tasks into the Conductor’s processing loop.

- `rox/frontend_client.py`
  - Sends typed UI actions to the frontend via RPC method `rox.interaction.ClientSideUI/PerformUIAction`.
  - Builds `AgentToClientUIActionRequest` proto messages via `utils/ui_action_factory.py`.
  - Convenience helpers: `set_ui_state`, `execute_visual_action`, `send_suggested_responses`, etc.

- `rox/browser_pod_client.py`
  - Publishes JSON messages on the LiveKit data channel to the browser pod participant (identity `browser-bot-<room>` or `browser-bot-<session_id>`).

- `rox/langgraph_client.py`
  - HTTP client to the Brain (LangGraph). Chooses endpoint:
    - `POST /start_session` for first turn
    - `POST /handle_interruption` when in interruption state
    - `POST /handle_response` for regular responses
  - Optionally augments requests with a live screenshot from `BROWSER_POD_HTTP_URL` or `VNC_LISTENER_HTTP_URL`.

- `rox/protos/interaction.proto`
  - Protocol buffers for all RPC types and UI actions exchanged over LiveKit RPC.
  - Generated Python lives in `rox/generated/protos/*`.

- `worker.py`
  - Entry for Cloud Run Job style execution: reads env (`LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, `LIVEKIT_ROOM_NAME`, etc.), constructs `WorkerOptions`, runs the agent.

- `local_test_server.py`
  - Minimal FastAPI utility that mirrors `/local-spawn-agent` for local development outside the main app.

## The Conductor pattern (`RoxAgent`)

- Maintains session context: `user_id`, `session_id`, `curriculum_id`, `current_lo_id`.
- Intercepts user speech (via `TranscriptInterceptor`) and debounces it; forwards to LangGraph as tasks.
- Consumes Brain responses shaped as a full state with a `delivery_plan`:
  - Executes the "toolbelt" — a sequence of actions such as `speak`, `draw`, `browser_navigate`, etc.
  - Sends SUGGESTED_RESPONSES to frontend when present (rich or legacy forms).
- Handles interruption logic: pauses current plan, handles the interruption turn, optionally resumes or discards.

## Data paths

1. Frontend speech/UI -> LiveKit -> Agent
   - Speech is transcribed (Deepgram STT) and intercepted by `TranscriptInterceptor`.
   - Rich events can arrive via LiveKit RPC handlers in `rpc_services.py`.

2. Agent -> Brain (LangGraph)
   - `LangGraphClient.invoke_langgraph_task()` POSTs to the configured Brain service.
   - Optionally includes `visual_context` from browser pod HTTP endpoint.

3. Agent -> Frontend UI (RPC)
   - `FrontendClient._send_rpc()` sends protobuf `AgentToClientUIActionRequest` to the viewer participant.

4. Agent -> Browser Pod (data channel)
   - `BrowserPodClient.send_browser_command()` sends JSON payloads over LiveKit data channel to `browser-bot-<room|session_id>`.

## Environments and modes

- Server mode (FastAPI): `python -m uvicorn rox.main:app --port 5005`
  - Use `/local-spawn-agent` to spawn a worker in background.

- Agent mode (CLI): `python rox/main.py connect --url <ws> --room <name> --api-key <key> --api-secret <secret>`
  - The LiveKit Agents CLI sets up `JobContext` and calls `entrypoint()`.

- Container mode (Dockerfile):
  - If `LIVEKIT_URL` and `ROOM_NAME` env are set, runs as agent: `python rox/main.py connect ...`.
  - Else runs FastAPI: `uvicorn rox.main:app --host 0.0.0.0 --port 8080`.

Note: outside Docker, code commonly uses `LIVEKIT_ROOM_NAME`; Dockerfile checks `ROOM_NAME`. Align in your deployment.
