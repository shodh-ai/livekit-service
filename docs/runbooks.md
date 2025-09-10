# Runbooks

## Start an agent for a room (HTTP dev server)
1. Run `uvicorn rox.main:app --port 5005`.
2. POST `/local-spawn-agent` with room details.
3. Verify logs show connection and participant join.

## Send a one‑off browser command
1. Ensure the browser pod is in the room with identity `browser-bot-<room>`.
2. POST `/dev/send-browser-command` with `{ room_name, tool_name, parameters }`.

## Rotate keys
1. Rotate `LIVEKIT_API_SECRET` and other keys in your secret manager.
2. Redeploy or restart services to pick up new env.
3. Verify `/` health and agent connection.

## Verify Brain integration
1. Check `LANGGRAPH_TUTOR_URL`.
2. Trigger a session start (e.g., via frontend or by queuing `start_tutoring_session`).
3. Confirm `LangGraphClient` logs a POST and receives a `delivery_plan`.

## Emergency stop of a noisy agent
1. From LiveKit admin UI, remove the agent participant from the room.
2. If running as job, terminate the job instance.
