# livekit-service/worker.py - Updated for Cloud Run Job execution
import os
import sys
import json
import logging
import asyncio
from livekit import agents
from rox.main import entrypoint

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def run_agent_from_env():
    """
    Configures and runs a single LiveKit agent based on environment variables
    set by the Cloud Run Job execution from Cloud Tasks.
    """
    try:
        # Read configuration from environment variables
        ws_url = os.environ.get("LIVEKIT_URL")
        api_key = os.environ.get("LIVEKIT_API_KEY")
        api_secret = os.environ.get("LIVEKIT_API_SECRET")
        room_name = os.environ.get("LIVEKIT_ROOM_NAME")
        agent_identity = os.environ.get("AGENT_IDENTITY")
        student_token_metadata = os.environ.get("STUDENT_TOKEN_METADATA")
        deepgram_api_key = os.environ.get("DEEPGRAM_API_KEY")
        langgraph_url = os.environ.get("LANGGRAPH_TUTOR_URL")  # Add this

        
        # Validate required environment variables
        if not all([ws_url, api_key, api_secret, room_name]):
            missing = [var for var, val in {
                "LIVEKIT_URL": ws_url,
                "LIVEKIT_API_KEY": api_key,
                "LIVEKIT_API_SECRET": api_secret,
                "LIVEKIT_ROOM_NAME": room_name
            }.items() if not val]
            logger.error(f"Missing required environment variables: {missing}")
            sys.exit(1)
        
        logger.info(f"Starting agent for room: {room_name}")
        logger.info(f"Agent identity: {agent_identity}")
        logger.info(f"WebSocket URL: {ws_url}")
        logger.info(f"API Key: {api_key[:10]}...")
        logger.info(f"LangGraph URL: {langgraph_url}")  # Add this
        
        if student_token_metadata:
            logger.info(f"Student metadata: {student_token_metadata}")
        
        # Create new event loop for this process
        logger.info("Creating new event loop...")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        logger.info("Event loop created successfully")
        
        try:
            # Create worker with the provided configuration
            logger.info("Creating worker options...")
            worker_options = agents.WorkerOptions(
                entrypoint_fnc=entrypoint,
                ws_url=ws_url,
                api_key=api_key,
                api_secret=api_secret
            )
            logger.info("Worker options created successfully")
            
            logger.info("Creating worker instance...")
            worker = agents.Worker(worker_options)
            logger.info("Worker instance created successfully")
            
            logger.info(f"Starting LiveKit agent worker for room {room_name}")
            logger.info("About to call worker.run()...")
            loop.run_until_complete(worker.run())
            logger.info("worker.run() completed")
            
        except Exception as worker_error:
            logger.error(f"Error in worker execution: {worker_error}", exc_info=True)
            raise
        finally:
            logger.info("Closing event loop...")
            loop.close()
            logger.info("Event loop closed")

        logger.info(f"Agent for room {room_name} has finished successfully")

    except SystemExit as se:
        logger.info(f"Agent exited with code: {se.code}")
        sys.exit(se.code)
    except Exception as e:
        logger.error(f"Failed to run agent: {e}", exc_info=True)
        sys.exit(1)  # Exit with error code so Cloud Run knows it failed

if __name__ == "__main__":
    logger.info("LiveKit Agent Worker starting...")
    logger.info("Environment variables:")
    # Add LANGGRAPH_TUTOR_URL to the list
    for key in ["LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_ROOM_NAME", "AGENT_IDENTITY", "STUDENT_TOKEN_METADATA", "DEEPGRAM_API_KEY", "LANGGRAPH_TUTOR_URL"]:
        value = os.environ.get(key, "NOT_SET")
        if key == "LIVEKIT_API_KEY" and value != "NOT_SET":
            value = value[:10] + "..."
        logger.info(f"  {key}: {value}")
    
    logger.info("About to call run_agent_from_env()...")
    run_agent_from_env()
    logger.info("run_agent_from_env() completed")