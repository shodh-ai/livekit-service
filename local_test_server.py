# livekit-service/local_test_server.py
import logging
import uvicorn
import os
import sys
import threading
from fastapi import FastAPI, BackgroundTasks, HTTPException
from pydantic import BaseModel

# Add project root to path to allow imports from rox folder
project_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, project_root)

from livekit import agents
from livekit import api as lk_api
from rox.main import entrypoint  # Import your agent's entrypoint

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Pydantic model for the incoming request from webrtc-token-service ---
class AgentSpawnRequest(BaseModel):
    room_name: str
    ws_url: str
    api_key: str
    api_secret: str
    agent_identity: str
    student_token_metadata: str

# In-process guard to avoid spawning multiple workers for the same room
_room_locks = set()
_room_lock_mutex = threading.Lock()

# --- The function that runs the agent (this will be the background task) ---
def run_agent_process(payload: AgentSpawnRequest):
    """
    This function runs in a background thread to start the LiveKit agent
    """
    try:
        logger.info(f"[Background Task] Starting agent for room: {payload.room_name}")
        logger.info(f"[DEBUG] Environment variables being set:")
        logger.info(f"[DEBUG] LIVEKIT_URL: {payload.ws_url}")
        logger.info(f"[DEBUG] LIVEKIT_API_KEY: {payload.api_key[:10]}...")
        logger.info(f"[DEBUG] LIVEKIT_ROOM_NAME: {payload.room_name}")
        
        # Set environment variables for the agent's JobContext
        os.environ["LIVEKIT_URL"] = payload.ws_url
        os.environ["LIVEKIT_API_KEY"] = payload.api_key
        os.environ["LIVEKIT_API_SECRET"] = payload.api_secret
        os.environ["LIVEKIT_ROOM_NAME"] = payload.room_name
        os.environ["STUDENT_TOKEN_METADATA"] = payload.student_token_metadata
        
        logger.info(f"[DEBUG] About to start agent worker programmatically...")
        
        # Import asyncio to run the agent properly
        import asyncio
        
        # Create new event loop for this thread FIRST
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            # Now create the worker with proper options (with event loop set)
            worker_options = agents.WorkerOptions(
                entrypoint_fnc=entrypoint,
                ws_url=payload.ws_url,
                api_key=payload.api_key,
                api_secret=payload.api_secret
            )
            worker = agents.Worker(worker_options)

            async def _launch_simulated_job():
                try:
                    # Give the worker a brief moment to register
                    await asyncio.sleep(0.2)
                    # Build a join token directly to avoid room-creation race when the room already exists
                    join_jwt = (
                        lk_api.AccessToken(payload.api_key, payload.api_secret)
                        .with_identity(f"agent-{payload.room_name}")
                        .with_kind("agent")
                        .with_grants(
                            lk_api.VideoGrants(
                                room_join=True,
                                room=payload.room_name,
                                agent=True,
                            )
                        )
                        .to_jwt()
                    )
                    # Retry a few times until the job is active
                    max_attempts = 5
                    for attempt in range(1, max_attempts + 1):
                        try:
                            await worker.simulate_job(join_jwt)
                            logger.info(
                                f"[DEV] Simulated job launch attempt {attempt}/{max_attempts} for room {payload.room_name}"
                            )
                            # Wait briefly to see if it became active
                            await asyncio.sleep(0.5)
                            if len(worker.active_jobs) > 0:
                                logger.info(
                                    f"[DEV] Simulated job is active (jobs={len(worker.active_jobs)})"
                                )
                                break
                        except Exception as se:
                            logger.warning(
                                f"[DEV] simulate_job attempt {attempt} failed: {se}", exc_info=True
                            )
                            await asyncio.sleep(0.5)
                    else:
                        logger.warning("[DEV] simulate_job did not activate any job after retries")
                except Exception as e:
                    logger.warning(f"[DEV] Failed to launch simulated job: {e}", exc_info=True)

            # Start the worker and schedule the simulated job in parallel
            run_task = loop.create_task(worker.run())

            # Precise trigger: when the worker registers, fire simulated job once
            _registered_fired = {"val": False}

            def _on_registered(*args, **kwargs):
                if not _registered_fired["val"]:
                    _registered_fired["val"] = True
                    loop.create_task(_launch_simulated_job())

            try:
                worker.on("worker_registered", _on_registered)
            except Exception:
                # Fallback if .on not available: timed attempt
                loop.create_task(_launch_simulated_job())

            # Also keep a timed fallback in case event misses
            loop.call_later(1.0, lambda: (not _registered_fired["val"]) and loop.create_task(_launch_simulated_job()))
            loop.run_until_complete(run_task)
        finally:
            loop.close()
        
        logger.info(f"[Background Task] Agent for room {payload.room_name} has finished.")
    except SystemExit as se:
        logger.info(f"[Background Task] Agent exited with code: {se.code}")
    except Exception as e:
        logger.error(f"[Background Task] Agent process failed: {e}", exc_info=True)
    finally:
        # Clear room lock so future spawns for this room can proceed
        try:
            with _room_lock_mutex:
                _room_locks.discard(payload.room_name)
        except Exception:
            pass

# --- FastAPI App ---
app = FastAPI(title="Local LiveKit Agent Worker")

@app.post("/local-spawn-agent")
async def spawn_agent(request: AgentSpawnRequest, background_tasks: BackgroundTasks):
    """
    This endpoint mimics Cloud Tasks triggering a Cloud Run worker.
    It immediately returns a response and starts the agent in the background.
    """
    logger.info(f"Received local spawn request for room: {request.room_name}")

    # Prevent duplicate workers for the same room within this process
    with _room_lock_mutex:
        if request.room_name in _room_locks:
            logger.info(
                f"Spawn request ignored: worker already running for room '{request.room_name}'."
            )
            return {
                "status": "accepted",
                "message": f"Worker already active for room {request.room_name}; no new spawn.",
            }
        _room_locks.add(request.room_name)

    # Add the long-running agent process as a background task
    background_tasks.add_task(run_agent_process, request)

    # Immediately return a success response to webrtc-token-service
    return {
        "status": "accepted",
        "message": f"Agent spawn task for room {request.room_name} is scheduled.",
    }

@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "local-livekit-agent-worker"}

if __name__ == "__main__":
    # Run the server on a port that doesn't conflict with your other services
    uvicorn.run(app, host="0.0.0.0", port=5005)
