"""
Live test agent that connects to a LiveKit room and lets you send manual browser commands
to the browser pod over the data channel.

Usage:
  LIVEKIT_ROOM_NAME=local-test-room python test_rox_agent.py

It fetches a token and wsUrl from the local token service (http://localhost:3002/api/token).
"""

import asyncio
import os
import json
import urllib.parse
import urllib.request
from livekit import rtc
from rox.browser_pod_client import BrowserPodClient


def fetch_dev_token(room_name: str, identity: str, base_url: str = "http://localhost:3002"):
    """Fetch a LiveKit token and wsUrl from the local token service using the dev GET endpoint."""
    qs = urllib.parse.urlencode({"room": room_name, "username": identity})
    url = f"{base_url.rstrip('/')}/api/token?{qs}"
    with urllib.request.urlopen(url) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    token = data.get("token")
    ws_url = data.get("wsUrl") or data.get("livekitUrl")
    if not token or not ws_url:
        raise RuntimeError(f"Token service response missing fields: {data}")
    return token, ws_url


async def main():
    room_name = os.environ.get("LIVEKIT_ROOM_NAME", "local-test-room")
    agent_identity = f"agent-{room_name}"

    # Fetch token/wsUrl from local dev token service
    token_service_url = os.environ.get("TOKEN_SERVICE_URL", "http://localhost:3002")
    token, livekit_url = await asyncio.to_thread(fetch_dev_token, room_name, agent_identity, token_service_url)

    room = rtc.Room()
    await room.connect(livekit_url, token)
    print(f"--- Mock RoxAgent connected to room: {room_name} ---")

    browser_client = BrowserPodClient()
    browser_pod_identity = f"browser-bot-{room_name}"

    print("Enter commands for the browser pod (e.g., 'navigate https://google.com')")
    print("Type 'exit' to quit.")

    while True:
        command_str = await asyncio.to_thread(input, "> ")
        if command_str.strip() == "":
            continue
        if command_str.lower() == 'exit':
            break

        parts = command_str.split()
        cmd = parts[0].lower()
        tool_name = f"browser_{cmd}"
        params = {}
        try:
            if cmd == "navigate" and len(parts) >= 2:
                params = {"url": parts[1]}
            elif cmd == "type" and len(parts) >= 3:
                params = {"selector": parts[1], "text": " ".join(parts[2:])}
            elif cmd == "click" and len(parts) == 3:
                params = {"x": int(parts[1]), "y": int(parts[2])}
            else:
                print("Unsupported or malformed command. Examples:\n  navigate https://google.com\n  type input[name=\"q\"] hello world\n  click 200 300")
                continue
            await browser_client.send_browser_command(room, browser_pod_identity, tool_name, params)
            print(f"Sent {tool_name} {params} to {browser_pod_identity}")
        except Exception as e:
            print(f"Error sending command: {e}")

    await room.disconnect()


if __name__ == "__main__":
    asyncio.run(main())