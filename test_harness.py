# test_harness.py
# A developer tool to send fixed toolbelts directly to the LiveKit Conductor (RoxAgent)
# and bypass the AI Brain. Useful to validate the end-to-end data-channel and execution
# pipeline between the Conductor and the Browser Pod.

import asyncio
import os
import json
import base64
import sys
from typing import Optional

from livekit import rtc
from livekit.api import AccessToken, VideoGrants

# --- Protobuf import (robust) ---
# Preferred: keep a copy of interaction_pb2.py alongside this script.
# Fallbacks: try to import from the repo path livekit-service/rox/generated/protos/
interaction_pb2 = None  # type: ignore
try:
    import interaction_pb2 as _pb2  # local copy next to this file
    interaction_pb2 = _pb2
except Exception:
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        repo_proto_dir = os.path.join(here, "livekit-service", "rox", "generated", "protos")
        if repo_proto_dir not in sys.path:
            sys.path.insert(0, repo_proto_dir)
        import interaction_pb2 as _pb2  # from repo
        interaction_pb2 = _pb2
    except Exception:
        pass

if interaction_pb2 is None:
    print("\nERROR: Could not import 'interaction_pb2'.")
    print("Please copy it from 'livekit-service/rox/generated/protos/interaction_pb2.py' into the same directory as this script,")
    print("or run this script from the repository root so the fallback import path works.\n")
    sys.exit(1)

# --- Configuration ---
LIVEKIT_URL = os.environ.get("LIVEKIT_URL")  # e.g., ws://localhost:7880
LIVEKIT_API_KEY = os.environ.get("LIVEKIT_API_KEY")
LIVEKIT_API_SECRET = os.environ.get("LIVEKIT_API_SECRET")
ROOM_NAME = os.environ.get("ROOM_NAME") or os.environ.get("LIVEKIT_ROOM_NAME")
AGENT_IDENTITY = os.environ.get("AGENT_IDENTITY", "rox-agent")  # set to your conductor's identity

# --- Pre-defined Toolbelts for Testing ---
TOOLBELT_BROWSER_BASICS = {
    "actions": [
        {"tool_name": "speak", "parameters": {"text": "Testing basic browser navigation and typing."}},
        {"tool_name": "browser_navigate", "parameters": {"url": "https://www.google.com/search?q=livekit"}},
        {"tool_name": "wait", "parameters": {"ms": 2000}},
        {"tool_name": "browser_type", "parameters": {"selector": "textarea[name=q]", "text": " agents framework"}},
        {"tool_name": "wait", "parameters": {"ms": 1000}},
        {"tool_name": "browser_key_press", "parameters": {"key": "Enter"}},
        {"tool_name": "speak", "parameters": {"text": "Browser test complete."}},
    ]
}

# Note: The following VS Code actions require a cooperating Browser Pod or Frontend that
# understands these commands. If your current pod doesn’t handle them yet, this test will
# still navigate to the page but the vscode_* actions may no-op.
TOOLBELT_VSCODE_TEST = {
    "actions": [
        {"tool_name": "speak", "parameters": {"text": "Now testing VS Code integration. I will navigate to the editor."}},
        {"tool_name": "browser_navigate", "parameters": {"url": "http://localhost:4600"}},
        {"tool_name": "wait", "parameters": {"ms": 5000}},
        {"tool_name": "speak", "parameters": {"text": "Creating a new file and adding content."}},
        {"tool_name": "vscode_add_line", "parameters": {"path": "test_from_harness.txt", "lineNumber": 1, "content": "# Hello from the test harness!"}},
        {"tool_name": "wait", "parameters": {"ms": 2000}},
        {"tool_name": "vscode_run_terminal_command", "parameters": {"command": "echo 'Terminal test successful!'"}},
        {"tool_name": "speak", "parameters": {"text": "VS Code test complete."}},
    ]
}


async def connect_and_wait_for_agent(room: rtc.Room, agent_identity: str, timeout_sec: float = 15.0) -> Optional[rtc.RemoteParticipant]:
    """Wait for a remote participant with the given identity to be present."""
    # Quick scan
    for p in room.remote_participants.values():
        if p.identity == agent_identity:
            return p
    # Wait loop
    end = asyncio.get_event_loop().time() + max(0.0, timeout_sec)
    while asyncio.get_event_loop().time() < end:
        for p in room.remote_participants.values():
            if p.identity == agent_identity:
                return p
        await asyncio.sleep(0.2)
    return None


async def run_tests(room: rtc.Room, agent_identity: str):
    tests_to_run = {
        "Browser Basics Test": TOOLBELT_BROWSER_BASICS,
        "VS Code Integration Test": TOOLBELT_VSCODE_TEST,
    }

    for name, toolbelt in tests_to_run.items():
        print(f"\n--- RUNNING TEST: {name} ---")

        # 1. Build protobuf request
        request_pb = interaction_pb2.InvokeAgentTaskRequest(
            task_name="__DIRECT_EXECUTE_TOOLBELT",
            json_payload=json.dumps(toolbelt),
        )

        # 2. Serialize & encode payload
        payload_bytes = request_pb.SerializeToString()
        b64_payload = base64.b64encode(payload_bytes).decode("utf-8")

        # 3. Send RPC to the agent
        print("Sending toolbelt to agent...")
        await room.local_participant.perform_rpc(
            destination_identity=agent_identity,
            method="rox.interaction.AgentInteraction/InvokeAgentTask",
            payload=b64_payload,
        )
        print("Command sent. Waiting for execution...")

        # Wait for actions to complete; adjust as needed per toolbelt
        await asyncio.sleep(20)

    print("\n--- All tests complete. ---")


async def main():
    if not all([LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET, ROOM_NAME]):
        print("Error: Missing required environment variables (LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET, ROOM_NAME)")
        return

    # Create a token for this script to join and publish data
    token = (
        AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity("test-harness")
        .with_name("Test Harness")
        .with_grants(VideoGrants(room_join=True, room=ROOM_NAME, can_publish_data=True))
        .to_jwt()
    )

    room = rtc.Room()
    try:
        print(f"Connecting to room '{ROOM_NAME}'...")
        await room.connect(LIVEKIT_URL, token)
        print("Successfully connected.")

        # Wait for the conductor agent to be present
        print(f"Waiting for agent identity '{AGENT_IDENTITY}' to be present...")
        agent_participant = await connect_and_wait_for_agent(room, AGENT_IDENTITY, timeout_sec=20.0)
        if not agent_participant:
            print(f"Error: Agent with identity '{AGENT_IDENTITY}' not found in the room.")
            print("Hint: Set AGENT_IDENTITY env var to the agent participant identity, or verify the agent is connected.")
            return
        print(f"Agent '{AGENT_IDENTITY}' found. Starting tests in 5 seconds...")
        await asyncio.sleep(5)

        await run_tests(room, AGENT_IDENTITY)

    finally:
        try:
            await room.disconnect()
        except Exception:
            pass
        print("Disconnected.")


if __name__ == "__main__":
    asyncio.run(main())
