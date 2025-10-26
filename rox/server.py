# rox/server.py
import asyncio
import logging
import os
import subprocess
import sys
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks
from opentelemetry import trace
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

logger = logging.getLogger(__name__)
settings = get_settings()
tracer = trace.get_tracer(__name__)

# Track running agent subprocesses per room
running_agents: Dict[str, subprocess.Popen] = {}
running_agents_lock: Optional[asyncio.Lock] = None

app = FastAPI(title="Rox Agent Service", description="Unified LiveKit Agent and HTTP API")
try:
    FastAPIInstrumentor.instrument_app(app)
except Exception as e:
    logger.error(f"Failed to instrument FastAPI app: {e}")


class AgentRequest(BaseModel):
    room_name: str
    room_url: str
    student_token_metadata: Optional[str] = None


@app.get("/")
def read_root():
    return {"service": "Rox Agent Service", "status": "running"}


@app.post("/run-agent")
async def run_agent(agent_request: AgentRequest, background_tasks: BackgroundTasks):
    room_name = agent_request.room_name
    room_url = agent_request.room_url
    api_key = settings.LIVEKIT_API_KEY
    api_secret = settings.LIVEKIT_API_SECRET

    if not all([api_key, api_secret]):
        raise HTTPException(status_code=500, detail="Server configuration error: LIVEKIT_API_KEY and LIVEKIT_API_SECRET must be set.")

    global running_agents_lock, running_agents
    if running_agents_lock is None:
        running_agents_lock = asyncio.Lock()

    async with running_agents_lock:
        try:
            finished_rooms = [r for r, proc in running_agents.items() if proc.poll() is not None]
            for r in finished_rooms:
                del running_agents[r]
        except Exception:
            pass

        if room_name in running_agents:
            try:
                old_proc = running_agents[room_name]
                old_proc.terminate()
                old_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                old_proc.kill()
            except Exception:
                pass
            finally:
                if room_name in running_agents:
                    del running_agents[room_name]

        def _launch_agent_process():
            try:
                env = os.environ.copy()
                env["LIVEKIT_URL"] = room_url
                env["LIVEKIT_ROOM_NAME"] = room_name
                env["LIVEKIT_API_KEY"] = api_key or ""
                env["LIVEKIT_API_SECRET"] = api_secret or ""
                if agent_request.student_token_metadata:
                    env["STUDENT_TOKEN_METADATA"] = agent_request.student_token_metadata
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
            except Exception:
                logger.error("Failed launching agent subprocess", exc_info=True)

        # trace scheduling
        with tracer.start_as_current_span("api.run_agent", attributes={"room": room_name}):
            background_tasks.add_task(_launch_agent_process)
            return {"message": f"Agent scheduled for room {room_name}"}


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
