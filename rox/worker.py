import os
import json
import time
import uuid
import subprocess
import sys
import threading
import logging
import redis
from typing import Any, Dict, Optional

from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


def _redis_client() -> Optional[redis.Redis]:
    try:
        host = settings.REDIS_HOST or os.environ.get("REDIS_HOST", "localhost")
        port = int(settings.REDIS_PORT or int(os.environ.get("REDIS_PORT", "6379")))
        client = redis.Redis(host=host, port=port, db=0, decode_responses=True)
        client.ping()
        return client
    except Exception:
        logger.error("Worker could not connect to Redis", exc_info=True)
        return None


def _spawn_agent(room_name: str, room_url: str, api_key: str, api_secret: str, student_token_metadata: Optional[str]) -> subprocess.Popen:
    env = os.environ.copy()
    env["LIVEKIT_URL"] = room_url
    env["LIVEKIT_ROOM_NAME"] = room_name
    env["LIVEKIT_API_KEY"] = api_key or ""
    env["LIVEKIT_API_SECRET"] = api_secret or ""
    if student_token_metadata:
        env["STUDENT_TOKEN_METADATA"] = student_token_metadata
    command = [
        sys.executable,
        os.path.join(os.path.dirname(__file__), "main.py"),
        "connect",
        "--room",
        room_name,
        "--url",
        room_url,
        "--api-key",
        api_key or "",
        "--api-secret",
        api_secret or "",
    ]
    return subprocess.Popen(command, env=env)


def _monitor_session(client: redis.Redis, worker_id: str, room_name: str, proc: subprocess.Popen, lock_key: str, lock_value: str) -> None:
    owner_key = f"room-owner:{room_name}"
    try:
        while proc.poll() is None:
            try:
                v = client.get(owner_key)
                if v == worker_id:
                    client.expire(owner_key, int(settings.ROOM_OWNER_TTL_SEC))
            except Exception:
                pass
            try:
                if client.get(lock_key) == lock_value:
                    client.expire(lock_key, int(settings.AGENT_LOCK_TTL_SEC))
            except Exception:
                pass
            time.sleep(int(settings.AGENT_LOCK_HEARTBEAT_SEC))
    finally:
        try:
            lua = """
            if redis.call('get', KEYS[1]) == ARGV[1] then
                return redis.call('del', KEYS[1])
            else
                return 0
            end
            """
            try:
                client.eval(lua, 1, owner_key, worker_id)
            except Exception:
                pass
            try:
                client.eval(lua, 1, lock_key, lock_value)
            except Exception:
                pass
        except Exception:
            pass


def _handle_start_agent(client: redis.Redis, worker_id: str, job: Dict[str, Any]) -> None:
    logger.info(f"Worker '{worker_id}' received start_agent job for room: {job.get('room_name')}")
    room_name = job.get("room_name")
    room_url = job.get("room_url")
    api_key = job.get("api_key", "")
    api_secret = job.get("api_secret", "")
    stm = job.get("student_token_metadata")
    lock_key = job.get("lock_key")
    lock_value = job.get("lock_value")
    if not (room_name and room_url and lock_key and lock_value):
        return
    owner_key = f"room-owner:{room_name}"
    try:
        client.set(owner_key, worker_id, ex=int(settings.ROOM_OWNER_TTL_SEC))
    except Exception:
        pass
    proc = _spawn_agent(room_name, room_url, api_key, api_secret, stm)
    t = threading.Thread(target=_monitor_session, args=(client, worker_id, room_name, proc, lock_key, lock_value), daemon=True)
    t.start()


def _handle_room_event(_client: redis.Redis, _worker_id: str, _job: Dict[str, Any]) -> None:
    return


def run_worker() -> None:
    client = _redis_client()
    if not client:
        raise RuntimeError("Redis not available for worker")
    worker_id = os.environ.get("WORKER_ID") or f"worker-{uuid.uuid4()}"
    try:
        client.sadd("active-workers", worker_id)
        client.set(f"worker-heartbeat:{worker_id}", "1", ex=90)
    except Exception:
        logger.error("Failed to register worker", exc_info=True)
        raise
    queues = [f"{settings.JOB_QUEUE_PREFIX}{worker_id}", settings.JOB_QUEUE_GENERAL]
    while True:
        try:
            try:
                client.set(f"worker-heartbeat:{worker_id}", "1", ex=90)
            except Exception:
                pass
            item = client.brpop(queues, timeout=5)
            if not item:
                continue
            _queue, payload = item
            try:
                job = json.loads(payload)
            except Exception:
                continue
            typ = job.get("type")
            if typ == "start_agent":
                _handle_start_agent(client, worker_id, job)
            elif typ == "room_event":
                _handle_room_event(client, worker_id, job)
        except KeyboardInterrupt:
            break
        except Exception:
            time.sleep(1)
    try:
        client.srem("active-workers", worker_id)
    except Exception:
        pass
