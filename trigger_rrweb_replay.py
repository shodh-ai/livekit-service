# file: trigger_rrweb_replay.py
import os
import asyncio
import base64
from typing import Optional, List
from livekit import rtc
from livekit.api import AccessToken, VideoGrants
from livekit.rtc.rpc import RpcError

try:
    # Optional: only needed if you pass gs:// URLs
    from rox.utils.gcs_signer import generate_v4_signed_url  # type: ignore
except Exception:
    generate_v4_signed_url = None  # type: ignore

def _normalize_url(url: str) -> str:
    # Remove stray backslashes and accidental '/?' before query
    s = (url or "").strip()
    # Common copy-paste escaping artifacts
    s = s.replace("\\?", "?").replace("\\&", "&").replace("\\=", "=")
    # If someone double-escaped, collapse backslashes
    s = s.replace("\\", "")
    # Fix '/?' to '?'
    if "/?" in s:
        s = s.replace("/?", "?")
    return s

def _maybe_sign_gcs_url(raw_url: str, ttl: int = 600) -> str:
    """If raw_url is gs://bucket/object, attempt to return a signed https URL."""
    if not raw_url.startswith("gs://"):
        return raw_url
    if generate_v4_signed_url is None:
        print("[warn] google-cloud-storage not available; cannot sign gs:// URL. Pass https signed URL instead.")
        return raw_url
    without = raw_url[5:]
    parts = without.split("/", 1)
    if len(parts) != 2:
        return raw_url
    bucket, obj = parts[0], parts[1]
    try:
        url = generate_v4_signed_url(bucket, obj, expiration_seconds=max(60, int(ttl)))
        if url:
            return url
    except Exception as e:
        print(f"[warn] Failed to sign gs:// URL: {e}")
    return raw_url

# Minimal RPC sender to the Frontend
# Reuses the same format as in FrontendClient.trigger_rrweb_replay
async def trigger_rrweb(room: rtc.Room, target_identity: str, events_url: str) -> bool:
    # Full RPC method that the frontend listens to (see FrontendClient)
    rpc_method = "rox.interaction.ClientSideUI/PerformUIAction"

    # Build the encoded protobuf payload using generated modules
    # We'll inline a tiny protobuf packer to avoid importing the whole repo.
    # The frontend accepts action_type=RRWEB_REPLAY(73) and parameters.events_url
    from rox.generated.protos import interaction_pb2

    req = interaction_pb2.AgentToClientUIActionRequest(
        action_type=interaction_pb2.ClientUIActionType.Value("RRWEB_REPLAY"),
        parameters={"events_url": events_url},
    )
    payload_b64 = base64.b64encode(req.SerializeToString()).decode("utf-8")
    resp_b64 = await room.local_participant.perform_rpc(
        destination_identity=target_identity,
        method=rpc_method,
        payload=payload_b64,
    )
    resp_bytes = base64.b64decode(resp_b64)
    resp = interaction_pb2.ClientUIActionResponse()
    resp.ParseFromString(resp_bytes)
    return bool(resp.success)

async def main():
    room_name = os.environ.get("ROOM_NAME")  # set this
    ws_url = os.environ.get("LIVEKIT_URL")
    api_key = os.environ.get("LIVEKIT_API_KEY")
    api_secret = os.environ.get("LIVEKIT_API_SECRET")
    events_url = os.environ.get("EVENTS_URL")  # set this

    if not all([room_name, ws_url, api_key, api_secret, events_url]):
        raise RuntimeError("Missing ROOM_NAME, LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET, or EVENTS_URL")

    # Mint a short-lived token for this command relay
    identity = f"rrweb-tester-{room_name}"
    token = (
        AccessToken(api_key, api_secret)
        .with_identity(identity)
        .with_name(identity)
        .with_grants(VideoGrants(room_join=True, room=room_name, can_publish_data=True))
        .to_jwt()
    )

    # Normalize and optionally sign the events URL
    events_url = _normalize_url(events_url)
    try:
        ttl = int(os.environ.get("GCS_SIGNED_URL_TTL", "600"))
    except Exception:
        ttl = 600
    events_url = _maybe_sign_gcs_url(events_url, ttl)

    room = rtc.Room()
    await room.connect(ws_url, token)
    try:
        # Allow manual override
        target = os.environ.get("TARGET_IDENTITY")

        # Small delay to let frontend register RPC handlers
        await asyncio.sleep(0.8)

        # Build candidate identities (exclude browser-bot)
        def list_candidates() -> List[str]:
            cands: List[str] = []
            for pid, participant in room.remote_participants.items():
                ident = str(getattr(participant, "identity", "") or pid)
                if ident.startswith("browser-bot-"):
                    continue
                cands.append(ident)
            return cands

        attempts = 5
        backoff = 0.8
        last_err: Optional[Exception] = None

        for attempt in range(1, attempts + 1):
            identities = [target] if target else list_candidates()
            # Dedup and remove None
            identities = [x for x in dict.fromkeys(identities) if x]
            if not identities:
                last_err = RuntimeError("No non-browser participants found yet")
            for ident in identities:
                try:
                    ok = await trigger_rrweb(room, ident, events_url)
                    print(f"RRWEB_REPLAY dispatched to {ident}: {ok}")
                    await asyncio.sleep(0.4)
                    return
                except RpcError as e:
                    # Method not supported yet or wrong destination; try next
                    last_err = e
                    continue
                except Exception as e:
                    last_err = e
                    continue
            if attempt < attempts:
                await asyncio.sleep(backoff)
                backoff *= 1.3
        # If we reached here, all attempts failed
        if last_err:
            raise last_err
        else:
            raise RuntimeError("Failed to dispatch RRWEB_REPLAY: no suitable destination")
    finally:
        try:
            await room.disconnect()
        except:
            pass

if __name__ == "__main__":
    asyncio.run(main())