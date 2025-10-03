import os
import json
import logging
from typing import Any, Dict, Optional

from redis.asyncio import Redis

_logger = logging.getLogger(__name__)

_redis: Optional[Redis] = None


def get_redis() -> Redis:
    """Lazily initialize and return a singleton async Redis client.

    Env vars:
    - REDIS_HOST (default: localhost)
    - REDIS_PORT (default: 6379)
    - REDIS_DB (default: 0)
    - REDIS_PASSWORD (optional)
    - REDIS_TLS (true/false, default: false)
    """
    global _redis
    if _redis is None:
        host = os.getenv("REDIS_HOST", "localhost")
        port = int(os.getenv("REDIS_PORT", "6379"))
        db = int(os.getenv("REDIS_DB", "0"))
        password = os.getenv("REDIS_PASSWORD") or None
        use_tls = (os.getenv("REDIS_TLS", "false").lower() in ("1", "true", "yes"))
        _redis = Redis(
            host=host,
            port=port,
            db=db,
            password=password,
            ssl=use_tls,
            decode_responses=True,
        )
        _logger.info(
            f"Redis client initialized host={host} port={port} db={db} tls={use_tls}"
        )
    return _redis


def session_state_key(session_id: str) -> str:
    return f"session:{session_id}:state"


async def read_tutor_state(session_id: str) -> Dict[str, Any]:
    """Read the session TutorState hash from Redis and JSON-decode fields when possible."""
    r = get_redis()
    raw = await r.hgetall(session_state_key(session_id))
    result: Dict[str, Any] = {}
    for k, v in raw.items():
        if k in ("current_step_index",):
            try:
                result[k] = int(v)
            except Exception:
                result[k] = v
            continue
        # Try JSON decode for complex fields
        try:
            result[k] = json.loads(v)
        except Exception:
            result[k] = v
    return result


async def write_tutor_state(session_id: str, mapping: Dict[str, Any], ttl_sec: Optional[int] = None) -> None:
    """Write fields into the session TutorState hash with optional TTL/expire.

    Lists/dicts are JSON-encoded. Primitives are stringified.
    """
    if not session_id:
        return
    r = get_redis()
    enc = {}
    for k, v in mapping.items():
        if v is None:
            continue
        if isinstance(v, (dict, list)):
            enc[k] = json.dumps(v)
        else:
            enc[k] = str(v)
    if enc:
        await r.hset(session_state_key(session_id), mapping=enc)
    if ttl_sec and ttl_sec > 0:
        await r.expire(session_state_key(session_id), ttl_sec)
