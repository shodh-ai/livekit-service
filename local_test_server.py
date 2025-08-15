# livekit-service/local_test_server.py
import logging
import uvicorn
import os
import sys
from fastapi import FastAPI, BackgroundTasks, HTTPException
from pydantic import BaseModel

# Add project root to path to allow imports from rox folder
project_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, project_root)

from livekit import agents
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
            loop.run_until_complete(worker.run())
        finally:
            loop.close()
        
        logger.info(f"[Background Task] Agent for room {payload.room_name} has finished.")
    except SystemExit as se:
        logger.info(f"[Background Task] Agent exited with code: {se.code}")
    except Exception as e:
        logger.error(f"[Background Task] Agent process failed: {e}", exc_info=True)

# --- FastAPI App ---
app = FastAPI(title="Local LiveKit Agent Worker")

@app.post("/local-spawn-agent")
async def spawn_agent(request: AgentSpawnRequest, background_tasks: BackgroundTasks):
    """
    This endpoint mimics Cloud Tasks triggering a Cloud Run worker.
    It immediately returns a response and starts the agent in the background.
    """
    logger.info(f"Received local spawn request for room: {request.room_name}")
    
    # Add the long-running agent process as a background task
    background_tasks.add_task(run_agent_process, request)
    
    # Immediately return a success response to webrtc-token-service
    return {"status": "accepted", "message": f"Agent spawn task for room {request.room_name} is scheduled."}

@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "local-livekit-agent-worker"}

if __name__ == "__main__":
    # Run the server on a port that doesn't conflict with your other services
    uvicorn.run(app, host="0.0.0.0", port=5005)
