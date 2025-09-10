# Troubleshooting

## LiveKit connection errors
- Symptom: Unable to connect, or agent exits quickly.
- Check: `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, and room name. Verify the LiveKit host is reachable from your environment.

## Deepgram not configured
- Symptom: STT not working, agent logs missing Deepgram activity.
- Fix: Set `DEEPGRAM_API_KEY`.

## No UI actions on frontend
- Symptom: Frontend not receiving RPC actions.
- Check: The viewer participant identity matches the one used in agent when calling `FrontendClient` (e.g., the agent uses `self.caller_identity`). Ensure the participant is connected.
- Ensure protobuf compatibility between `interaction.proto` and frontend handlers. Regenerate if schema changed.

## Browser pod not responding
- Symptom: Data‑channel commands sent but no action.
- Check: Browser pod participant identity is `browser-bot-<room|session_id>`, matches what agent uses in `browser_pod_client.py`.
- Verify pod is connected to the same LiveKit room.
- Verify the pod’s data‑channel listener filters on `target` and `source`.

## LangGraph timeouts or errors
- Symptom: `/handle_response` returns non‑200 or times out.
- Check: `LANGGRAPH_TUTOR_URL` is reachable.
- Use logs around `LangGraphClient` to see request body/endpoint.
- Disable visual_context temporarily if screenshot endpoint is slow/unavailable.

## Protobuf issues
- Symptom: parse errors or attribute errors when handling RPC payloads.
- Fix: Regenerate Python stubs from `rox/protos/interaction.proto`.

## Docker CMD mode not starting agent
- Symptom: Container starts FastAPI instead of agent.
- Cause: Dockerfile checks `LIVEKIT_URL` and `ROOM_NAME` env. Ensure both are set in container env.
