# test_receiver.py
import asyncio
import os
import json
from livekit import rtc
from livekit.api import AccessToken, VideoGrants
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

LIVEKIT_URL = os.environ.get("LIVEKIT_URL")
LIVEKIT_API_KEY = os.environ.get("LIVEKIT_API_KEY")
LIVEKIT_API_SECRET = os.environ.get("LIVEKIT_API_SECRET")

async def main(room_name: str):
    if not all([LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET]):
        print("Error: Missing LiveKit environment variables.")
        return

    # IMPORTANT: use a distinct identity so we don't kick the browser pod off the room
    identity = f"debug-receiver-{os.getpid()}-{room_name}"

    # Create a token to join the room
    token = (
        AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_name("TestReceiver")
        # We only need to join and receive; do not publish data to keep things simple
        .with_grants(VideoGrants(room_join=True, room=room_name, can_publish_data=False))
        .to_jwt()
    )

    room = rtc.Room()

    @room.on("data_received")
    def on_data_received(data: rtc.DataPacket):
        payload = data.data.decode("utf-8")
        print("\n✅ DATA RECEIVED! ✅")
        print(f"From participant: {data.participant.identity}")
        try:
            parsed_json = json.loads(payload)
            print("Payload (JSON):")
            print(json.dumps(parsed_json, indent=2))
        except json.JSONDecodeError:
            print(f"Payload (Raw Text): {payload}")
        print("--------------------")

    try:
        print(f"Connecting to room '{room_name}' as '{identity}'...")
        await room.connect(LIVEKIT_URL, token)
        print("Connection successful. Waiting for data packets...")
        await asyncio.Event().wait()  # Keep running indefinitely
    except Exception as e:
        print(f"Failed to connect: {e}")
    finally:
        await room.disconnect()

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python test_receiver.py <room_name>")
    else:
        asyncio.run(main(sys.argv[1]))