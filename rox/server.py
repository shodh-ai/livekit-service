# rox/server.py
import asyncio
import logging
import os
import subprocess
import sys
import redis
import uuid
import json
import time
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from opentelemetry import trace
from opentelemetry import metrics
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from pydantic import BaseModel
from livekit import rtc, agents

try:
    from .entrypoint import entrypoint
    from .agent import RoxAgent  # re-exported reference if needed elsewhere
except Exception:
    from entrypoint import entrypoint
    from agent import RoxAgent  # type: ignore
from browser_pod_client import BrowserPodClient
from config import get_settings
try:
    from logging_setup import install_logging_filter
    from request_context import set_request_context
except Exception:
    pass

logger = logging.getLogger(__name__)
settings = get_settings()
tracer = trace.get_tracer(__name__)

try:
    # Prefer REDIS_URL if provided (e.g. Upstash rediss:// URL); otherwise fall back
    # to host/port for local development.
    redis_url = settings.REDIS_URL or os.environ.get("REDIS_URL")
    if redis_url:
        redis_client = redis.from_url(
            redis_url,
            decode_responses=True,
            socket_timeout=5,
        )
    else:
        redis_host = settings.REDIS_HOST or os.environ.get("REDIS_HOST", "localhost")
        redis_port = int(settings.REDIS_PORT or int(os.environ.get("REDIS_PORT", "6379")))
        redis_client = redis.Redis(
            host=redis_host,
            port=redis_port,
            db=0,
            decode_responses=True,
            socket_timeout=5,
            socket_connect_timeout=5,
        )
    redis_client.ping()
    logger.info("Connected to Redis")
except Exception:
    logger.error("Redis connection failed", exc_info=True)
    redis_client = None

# Track running agent subprocesses per room
running_agents: Dict[str, subprocess.Popen] = {}
running_agents_lock: Optional[asyncio.Lock] = None

app = FastAPI(title="Rox Agent Service", description="Unified LiveKit Agent and HTTP API")
try:
    FastAPIInstrumentor.instrument_app(app)
except Exception as e:
    logger.error(f"Failed to instrument FastAPI app: {e}")
try:
    install_logging_filter()
except Exception:
    pass

try:
    _meter = metrics.get_meter(__name__)
    _m_lock_success = _meter.create_counter("lock_acquisition_success_count")
    _m_lock_failure = _meter.create_counter("lock_acquisition_failure_count")
    _m_heartbeat_fail = _meter.create_counter("heartbeat_failure_count")
    _m_agents_running = _meter.create_up_down_counter("agents_running_total")
except Exception:
    _meter = None
    _m_lock_success = None
    _m_lock_failure = None
    _m_heartbeat_fail = None
    _m_agents_running = None

class AgentRequest(BaseModel):
    room_name: str
    room_url: str
    student_token_metadata: Optional[str] = None


@app.get("/")
def read_root():
    return {"service": "Rox Agent Service", "status": "running"}

def _enqueue_job(queue: str, job: Dict[str, Any]) -> None:
    if not redis_client:
        raise RuntimeError("Redis not available")
    redis_client.rpush(queue, json.dumps(job, separators=(",", ":")))

def _launch_and_monitor_agent(
    room_name: str,
    room_url: str,
    api_key: str,
    api_secret: str,
    student_token_metadata: Optional[str],
    lock_key: str,
    lock_value: str,
):
    try:
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
        proc = subprocess.Popen(command, env=env)
        running_agents[room_name] = proc
        try:
            if _m_agents_running:
                _m_agents_running.add(1, attributes={"room_name": room_name})
        except Exception:
            pass
        while proc.poll() is None:
            try:
                if redis_client and redis_client.get(lock_key) == lock_value:
                    redis_client.expire(lock_key, int(settings.AGENT_LOCK_TTL_SEC))
            except Exception:
                logger.warning(f"Heartbeat failed for lock {lock_key}")
                try:
                    if _m_heartbeat_fail:
                        _m_heartbeat_fail.add(1, attributes={"room_name": room_name})
                except Exception:
                    pass
            time.sleep(int(settings.AGENT_LOCK_HEARTBEAT_SEC))
    except Exception:
        logger.error("Failed launching or monitoring agent subprocess", exc_info=True)
    finally:
        try:
            if room_name in running_agents:
                del running_agents[room_name]
            try:
                if _m_agents_running:
                    _m_agents_running.add(-1, attributes={"room_name": room_name})
            except Exception:
                pass
        finally:
            try:
                if redis_client:
                    lua = """
                    if redis.call('get', KEYS[1]) == ARGV[1] then
                        return redis.call('del', KEYS[1])
                    else
                        return 0
                    end
                    """
                    redis_client.eval(lua, 1, lock_key, lock_value)
            except Exception:
                logger.error("Failed to delete Redis lock on cleanup", exc_info=True)

# Middleware to capture session/student IDs and attach to current span
@app.middleware("http")
async def add_context_and_trace_attributes(request: Request, call_next):
    logger.info(f"HTTP {request.method} {request.url.path} starting")
    # For /run-agent, bypass JSON parsing and context capture to avoid interfering with body handling
    if request.url.path == "/run-agent":
        response = await call_next(request)
        try:
            logger.info(f"HTTP {request.method} {request.url.path} completed status={getattr(response, 'status_code', 'unknown')}")
        except Exception:
            pass
        return response

    session_id = None
    student_id = None
    try:
        body = await request.json()
        if isinstance(body, dict):
            session_id = body.get("session_id") or body.get("sessionId")
            student_id = body.get("student_id") or body.get("user_id") or body.get("studentId")
    except Exception:
        pass

    try:
        set_request_context(session_id, student_id)
    except Exception:
        pass

    try:
        span = trace.get_current_span()
        if span and span.is_recording():
            if session_id:
                span.set_attribute("session_id", session_id)
            if student_id:
                span.set_attribute("student_id", student_id)
    except Exception:
        pass

    response = await call_next(request)
    try:
        logger.info(f"HTTP {request.method} {request.url.path} completed status={getattr(response, 'status_code', 'unknown')}")
    except Exception:
        pass
    return response


def _run_agent_sync(room_name: str, room_url: str, student_token_metadata: Optional[str]) -> Dict[str, Any]:
    logger.info(f"/run-agent called for room_name={room_name} room_url={room_url}")
    if not redis_client:
        logger.error("run-agent: redis_client is None")
        raise HTTPException(status_code=503, detail="Service is not connected to Redis; cannot manage agents.")

    api_key = settings.LIVEKIT_API_KEY
    api_secret = settings.LIVEKIT_API_SECRET

    if not all([api_key, api_secret]):
        logger.error("run-agent: LIVEKIT_API_KEY/SECRET not set")
        raise HTTPException(status_code=500, detail="Server configuration error: LIVEKIT_API_KEY and LIVEKIT_API_SECRET must be set.")

    lock_key = f"agent-lock:{room_name}"
    instance_id = os.environ.get("K_REVISION", "local")
    lock_payload = {
        "instance_id": instance_id,
        "lock_id": str(uuid.uuid4()),
        "created_at": int(time.time()),
        "room_name": room_name,
    }
    lock_value = json.dumps(lock_payload, separators=(",", ":"))

    try:
        logger.info(f"run-agent: attempting to acquire lock {lock_key}")
        lock_acquired = redis_client.set(lock_key, lock_value, nx=True, ex=int(settings.AGENT_LOCK_TTL_SEC))
        logger.info(f"run-agent: lock acquired={bool(lock_acquired)} for {lock_key}")
    except Exception as e:
        logger.error(f"Redis SET failed for key {lock_key}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to acquire lock")

    if lock_acquired:
        try:
            if _m_lock_success:
                _m_lock_success.add(1, attributes={"room_name": room_name})
        except Exception:
            pass
        with tracer.start_as_current_span("api.run_agent", attributes={"room": room_name}):
            job = {
                "type": "start_agent",
                "room_name": room_name,
                "room_url": room_url,
                "api_key": api_key or "",
                "api_secret": api_secret or "",
                "student_token_metadata": student_token_metadata,
                "lock_key": lock_key,
                "lock_value": lock_value,
            }
            try:
                logger.info(f"run-agent: enqueueing job to {settings.JOB_QUEUE_GENERAL}")
                _enqueue_job(settings.JOB_QUEUE_GENERAL, job)
                logger.info(f"run-agent: job enqueued for room {room_name}")
            except Exception as e:
                logger.error(f"Failed to enqueue job: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail="Failed to enqueue job")
            return {"status": "accepted", "message": f"Agent scheduled for room {room_name}"}
    else:
        try:
            if _m_lock_failure:
                _m_lock_failure.add(1, attributes={"room_name": room_name})
        except Exception:
            pass
        try:
            holder = redis_client.get(lock_key)
            logger.info(f"Lock for room {room_name} held by {holder}")
        except Exception:
            pass
        return {"status": "no_action", "message": f"Agent is already running or starting for room {room_name}"}


@app.post("/run-agent")
async def run_agent(request: Request):
    """Schedule a LiveKit agent for a given room via Redis job queue.

    This parses the JSON body, then offloads the lock/queue work to a background
    thread via _run_agent_sync with a hard timeout, so the HTTP request cannot
    hang indefinitely.
    """
    logger.info("run-agent: handler entered")
    try:
        raw = await request.body()
        logger.info(f"run-agent: raw body={raw!r}")
        body = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception as e:
        logger.error(f"run-agent: failed to parse JSON body: {e}", exc_info=True)
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    if not isinstance(body, dict):
        logger.error(f"run-agent: body is not a JSON object: {body!r}")
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")

    room_name = body.get("room_name")
    room_url = body.get("room_url")
    student_token_metadata = body.get("student_token_metadata")

    if not room_name or not room_url:
        logger.error(f"run-agent: missing required fields in body: {body}")
        raise HTTPException(status_code=400, detail="Missing required fields 'room_name' or 'room_url'")

    timeout_sec = 10
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(_run_agent_sync, str(room_name), str(room_url), student_token_metadata),
            timeout=timeout_sec,
        )
        return result
    except asyncio.TimeoutError:
        logger.error(f"run-agent: timed out after {timeout_sec}s")
        raise HTTPException(status_code=500, detail="Timed out while scheduling agent")

class RoomEvent(BaseModel):
    room_name: str
    event_type: str
    payload: Optional[Dict[str, Any]] = None

@app.post("/room-event")
async def room_event(req: RoomEvent):
    if not redis_client:
        raise HTTPException(status_code=503, detail="Service is not connected to Redis")
    owner_key = f"room-owner:{req.room_name}"
    try:
        worker_id = redis_client.get(owner_key)
    except Exception as e:
        logger.error(f"Failed to resolve room owner: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to resolve room owner")
    if not worker_id:
        raise HTTPException(status_code=404, detail="No worker owns this room")
    queue = f"{settings.JOB_QUEUE_PREFIX}{worker_id}"
    job = {
        "type": "room_event",
        "room_name": req.room_name,
        "event_type": req.event_type,
        "payload": req.payload or {},
    }
    try:
        _enqueue_job(queue, job)
    except Exception as e:
        logger.error(f"Failed to enqueue room event: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to enqueue event")
    return {"status": "accepted"}

@app.get("/debug/locks")
async def debug_list_locks():
    if not settings.ENABLE_DEBUG_LOCKS_ENDPOINT:
        raise HTTPException(status_code=404, detail="Not found")
    if not redis_client:
        raise HTTPException(status_code=503, detail="Service is not connected to Redis")
    try:
        keys = list(redis_client.scan_iter("agent-lock:*"))
        items = []
        for k in keys:
            v = redis_client.get(k)
            t = redis_client.ttl(k)
            try:
                parsed = json.loads(v) if v else None
            except Exception:
                parsed = v
            items.append({"key": k, "value": parsed, "ttl": t})
        return {"count": len(items), "locks": items}
    except Exception as e:
        logger.error(f"/debug/locks failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to list locks")


class BrowserCommandRequest(BaseModel):
    room_name: str
    tool_name: str
    parameters: Optional[Dict[str, Any]] = None
    room_url: Optional[str] = None
    token: Optional[str] = None
    participant_identity: Optional[str] = None


@app.post("/dev/send-browser-command")
async def dev_send_browser_command(req: BrowserCommandRequest):
    try:
        room_name = req.room_name
        tool_name = req.tool_name
        params = req.parameters or {}
        ws_url = req.room_url or settings.LIVEKIT_URL
        token = req.token
        agent_identity = req.participant_identity or f"cmd-relay-{room_name}"
        if not token:
            api_key = settings.LIVEKIT_API_KEY
            api_secret = settings.LIVEKIT_API_SECRET
            if not (api_key and api_secret):
                raise HTTPException(status_code=500, detail="LIVEKIT_API_KEY/SECRET not set; cannot mint agent token. Pass 'token' explicitly.")
            try:
                from livekit.api import AccessToken, VideoGrants

                grants = VideoGrants(room_join=True, room=room_name, can_publish_data=True)
                token = (
                    AccessToken(api_key, api_secret)
                    .with_identity(agent_identity)
                    .with_name(agent_identity)
                    .with_grants(grants)
                    .to_jwt()
                )
            except Exception as e:
                logger.error(f"Failed to mint agent token: {e}")
                raise HTTPException(status_code=500, detail="Failed to mint agent token")
        if not ws_url:
            raise HTTPException(status_code=500, detail="Could not resolve LiveKit wsUrl. Set LIVEKIT_URL or pass room_url.")
        room = rtc.Room()
        with tracer.start_as_current_span("api.dev_send_browser_command", attributes={"room": room_name, "tool": tool_name}):
            await room.connect(ws_url, token)
        try:
            browser_identity = f"browser-bot-{room_name}"
            client = BrowserPodClient()
            ok = await client.send_browser_command(room, browser_identity, tool_name, params)
            await asyncio.sleep(0.4)
            return {"ok": bool(ok), "room": room.name, "tool": tool_name, "parameters": params}
        finally:
            try:
                await room.disconnect()
            except Exception:
                pass
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"/dev/send-browser-command failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


class AgentSpawnRequest(BaseModel):
    room_name: str
    ws_url: str
    api_key: str
    api_secret: str
    agent_identity: str
    student_token_metadata: str


async def _run_worker_from_payload(payload: AgentSpawnRequest):
    try:
        os.environ["LIVEKIT_URL"] = payload.ws_url
        os.environ["LIVEKIT_API_KEY"] = payload.api_key
        os.environ["LIVEKIT_API_SECRET"] = payload.api_secret
        os.environ["LIVEKIT_ROOM_NAME"] = payload.room_name
        os.environ["STUDENT_TOKEN_METADATA"] = payload.student_token_metadata
        worker_options = agents.WorkerOptions(
            entrypoint_fnc=entrypoint, ws_url=payload.ws_url, api_key=payload.api_key, api_secret=payload.api_secret
        )
        worker = agents.Worker(worker_options)
        await worker.run()
    except Exception as e:
        logger.error(f"[local-spawn-agent] Worker failed: {e}", exc_info=True)


@app.post("/local-spawn-agent")
async def local_spawn_agent(request: AgentSpawnRequest):
    try:
        with tracer.start_as_current_span("api.local_spawn_agent", attributes={"room": request.room_name}):
            asyncio.create_task(_run_worker_from_payload(request))
            return {"status": "accepted", "message": f"Agent spawn scheduled for room {request.room_name}"}
    except Exception as e:
        logger.error(f"/local-spawn-agent failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


def run_fastapi_server():
    import uvicorn

    port = int(settings.PORT)
    logger.info(f"Starting FastAPI server on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
