# livekit-service/worker.py
import os
import sys
import json
import logging
from livekit import agents
from rox.main import entrypoint
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class AgentStartRequest(BaseModel):
    room_name: str
    ws_url: str
    api_key: str
    api_secret: str
    agent_identity: str
    student_token_metadata: str

def run_agent_from_task(task_payload: dict):
    """
    Configures and runs a single LiveKit agent based on a task payload.
    Uses the same proven approach as local_test_server.py after debugging.
    """
    try:
        # Validate payload
        request = AgentStartRequest(**task_payload)
        
        logger.info(f"Worker received task. Starting agent for room: {request.room_name}")
        logger.info(f"[DEBUG] Environment variables being set:")
        logger.info(f"[DEBUG] LIVEKIT_URL: {request.ws_url}")
        logger.info(f"[DEBUG] LIVEKIT_API_KEY: {request.api_key[:10]}...")
        logger.info(f"[DEBUG] LIVEKIT_ROOM_NAME: {request.room_name}")

        # Set environment variables for the agent process
        os.environ["LIVEKIT_URL"] = request.ws_url
        os.environ["LIVEKIT_API_KEY"] = request.api_key
        os.environ["LIVEKIT_API_SECRET"] = request.api_secret
        os.environ["LIVEKIT_ROOM_NAME"] = request.room_name
        
        # Pass student metadata to the agent via an environment variable
        # This is a robust way to get context into the JobContext
        os.environ["STUDENT_TOKEN_METADATA"] = request.student_token_metadata

        logger.info(f"[DEBUG] About to start agent worker programmatically...")
        
        # Import asyncio to run the agent properly
        import asyncio
        
        # Create new event loop for this thread FIRST (critical fix from local_test_server)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            # Now create the worker with proper options (with event loop set)
            worker_options = agents.WorkerOptions(
                entrypoint_fnc=entrypoint,
                ws_url=request.ws_url,
                api_key=request.api_key,
                api_secret=request.api_secret
            )
            worker = agents.Worker(worker_options)
            loop.run_until_complete(worker.run())
        finally:
            loop.close()

        logger.info(f"Agent for room {request.room_name} has finished and worker is exiting.")

    except SystemExit as se:
        logger.info(f"Agent exited with code: {se.code}")
    except Exception as e:
        logger.error(f"Failed to run agent from task: {e}", exc_info=True)
        sys.exit(1) # Exit with error code so the task queue knows it failed

if __name__ == "__main__":
    # In a real Cloud Task worker, the payload comes from the POST body.
    # This is a simple example of how to read it from stdin.
    # Your Cloud Run service will simply parse the request body.
    
    # This part is more conceptual. Your Cloud Run instance would be a FastAPI
    # app with a single endpoint that receives the task and calls run_agent_from_task.
    logger.info("Agent worker started. Waiting for task...")
    # In a real scenario, a lightweight web framework like FastAPI would receive the POST
    # from Cloud Tasks here. For simplicity, we assume the task is passed.
