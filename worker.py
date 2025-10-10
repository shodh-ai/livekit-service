# livekit-service/worker.py - Updated for Cloud Run Job execution
import os
import sys
import json
import logging
import asyncio
from datetime import datetime, timedelta
from livekit import agents
from livekit.api import AccessToken, VideoGrants
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
        # Validate AGENT_IDENTITY explicitly
        if not agent_identity:
            logger.error("Missing required environment variable: AGENT_IDENTITY")
            sys.exit(1)

        logger.info(f"Starting agent for room: {room_name}")
        logger.info(f"Agent identity: {agent_identity}")
        logger.info(f"WebSocket URL: {ws_url}")
        logger.info(f"API Key: {api_key[:10]}...")
        logger.info(f"LangGraph URL: {langgraph_url}")  # Add this
        
        if student_token_metadata:
            logger.info(f"Student metadata: {student_token_metadata}")
        
        # Single-instance per-room guard within this container: lock file in /tmp
        lock_path = f"/tmp/rox_room_{room_name}.lock"
        lock_acquired = False
        try:
            # O_CREAT|O_EXCL to ensure exclusive creation
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, str(os.getpid()).encode())
            finally:
                os.close(fd)
            lock_acquired = True
            logger.info(f"Acquired room lock: {lock_path}")
        except FileExistsError:
            logger.info(
                f"Worker for room '{room_name}' appears active in this container (lock exists: {lock_path}). Exiting gracefully."
            )
            return

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
            logger.info("About to call worker.run() and simulate job...")

            async def _launch_and_run():
                # Give the worker a moment to register itself
                await asyncio.sleep(1)
                logger.info("Worker registered. Now simulating job to force connection...")

                # Create a token that gives this agent permission to join the room.
                join_token = (
                    AccessToken(api_key, api_secret)
                    .with_identity(agent_identity)
                    .with_grants(
                        VideoGrants(
                            room_join=True,
                            room=room_name,
                            agent=True,
                        )
                    )
                    .to_jwt()
                )

                # Command to start the job immediately
                await worker.simulate_job(join_token)
                logger.info(f"Job simulated for room '{room_name}'. Entrypoint should now be running.")

            # Run the worker and job launch concurrently
            run_task = loop.create_task(worker.run())
            launch_task = loop.create_task(_launch_and_run())

            # Wait for the worker to finish
            loop.run_until_complete(run_task)
            logger.info("worker.run() completed")
            
        except Exception as worker_error:
            logger.error(f"Error in worker execution: {worker_error}", exc_info=True)
            raise
        finally:
            logger.info("Closing event loop...")
            loop.close()
            logger.info("Event loop closed")
            # Release room lock
            try:
                if lock_acquired and os.path.exists(lock_path):
                    os.remove(lock_path)
                    logger.info(f"Released room lock: {lock_path}")
            except Exception:
                logger.debug("Failed to remove room lock (non-fatal)", exc_info=True)

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