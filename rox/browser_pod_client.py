# livekit-service/rox/browser_pod_client.py
from __future__ import annotations
from typing import Any, Dict, Optional
from livekit import rtc
import json

class BrowserPodClient:
    """Sends browser commands to the session-bubble pod over LiveKit data channel."""

    def __init__(self) -> None:
        pass

    async def send_browser_command(
        self,
        room: rtc.Room,
        browser_identity: str,
        tool_name: str,
        parameters: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if not room or not room.local_participant:
            return False

        payload = {
            # Keep tool_name unchanged for compatibility with the browser pod
            "tool_name": tool_name,
            "parameters": parameters or {},
            # Route only to the intended browser participant
            "target": browser_identity,
            # Explicit source marker for receivers (for future filtering)
            "source": "agent",
        }
        data = json.dumps(payload).encode("utf-8")

        # Publish reliably and route only to the intended browser-bot identity
        await room.local_participant.publish_data(
            data,
            reliable=True,
            destination_identities=[browser_identity],
        )
        return True
