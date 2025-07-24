#!/usr/bin/env python3
"""
Rox Assistant LiveKit Conductor - Refactored Architecture

This module implements the Conductor pattern for sophisticated real-time AI tutoring.
The Conductor orchestrates communication between the Brain (LangGraph), Body (UI actions),
and maintains the State of Expectation for intelligent interaction handling.
"""

import os
import sys
import logging
import asyncio
import json
from pathlib import Path
from typing import Optional, Dict, Any, List

# Add project root to path for clean imports
project_root = Path(__file__).resolve().parent
sys.path.insert(0, str(project_root))

from dotenv import load_dotenv

# LiveKit imports
from livekit import rtc, agents
from livekit.agents import Agent, JobContext, WorkerOptions
from livekit.plugins import deepgram, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

# Local application imports
from generated.protos import interaction_pb2
from rpc_services import AgentInteractionService
from langgraph_client import LangGraphClient
from frontend_client import FrontendClient

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# --- Environment Loading and Validation ---
load_dotenv(dotenv_path=project_root / ".env")

# Environment variables validation (skip during testing)
def validate_environment():
    """Validate required environment variables."""
    required_env_vars = [
        "LIVEKIT_API_KEY",
        "LIVEKIT_API_SECRET", 
        "DEEPGRAM_API_KEY"
    ]
    
    missing_vars = []
    for var in required_env_vars:
        if not os.getenv(var):
            missing_vars.append(var)
    
    if missing_vars:
        error_msg = f"Required environment variables not set: {', '.join(missing_vars)}"
        logger.error(error_msg)
        return False, error_msg
    
    logger.info("Environment validation completed successfully")
    return True, "All required environment variables are set"

# Only validate environment when not in test mode
def is_running_tests():
    """Check if we're running in a test environment."""
    import inspect
    for frame_info in inspect.stack():
        if 'pytest' in frame_info.filename or 'test_' in frame_info.filename:
            return True
    return False

if not is_running_tests():
    is_valid, message = validate_environment()
    if not is_valid:
        sys.exit(1)


class RoxAgent(Agent):
    """The Conductor - Central orchestrator for real-time AI tutoring.
    
    This class implements the Conductor pattern, managing:
    - State of Expectation (INTERRUPTION vs SUBMISSION)
    - Communication with Brain (LangGraph) and Body (Frontend)
    - Unified Action Executor for performing AI-generated scripts
    - Real-time processing queue for handling student interactions
    """
    
    def __init__(self, **kwargs):
        # Set default instructions if not provided, to satisfy Agent base class
        kwargs.setdefault(
            "instructions",
            "You are Rox, an AI tutor. You help students learn through interactive conversations and activities."
        )
        super().__init__(**kwargs)
        
        # --- State Management ---
        self.user_id: Optional[str] = None
        self.session_id: Optional[str] = None
        self.caller_identity: Optional[str] = None  # The participant ID of the frontend
        
        # State of Expectation - determines how to interpret student input
        self._expected_user_input_type: str = "INTERRUPTION"  # Default state
        
        # Processing queue for tasks from the Brain
        self._processing_queue: asyncio.Queue = asyncio.Queue()
        
        # --- Communication Clients ---
        self._langgraph_client = LangGraphClient()
        self._frontend_client = FrontendClient()
        
        # LiveKit components
        self._room: Optional[rtc.Room] = None
        self.agent_session: Optional[agents.AgentSession] = None
        
        logger.info("RoxAgent Conductor initialized")

    async def processing_loop(self):
        """The main engine of the Conductor.
        
        This loop continuously processes tasks from the queue:
        1. Receives task from RPC handlers
        2. Sends task to Brain (LangGraph) for processing
        3. Executes the returned script via Unified Action Executor
        """
        logger.info("Conductor's main processing loop started")
        
        while True:
            try:
                # Wait for a task to be queued
                task = await self._processing_queue.get()
                logger.info(f"Processing task: {task.get('task_name')}")

                # 1. Ask the Brain for the script (toolbelt)
                toolbelt = await self._langgraph_client.invoke_langgraph_task(
                    task=task,
                    user_id=self.user_id or "anonymous",
                    session_id=self.session_id or "default_session"
                )
                
                if not toolbelt:
                    logger.error("Received empty toolbelt from Brain. Skipping turn.")
                    continue

                # 2. Execute the script via Unified Action Executor
                await self._execute_toolbelt(toolbelt)
                
                # Mark task as done
                self._processing_queue.task_done()

            except Exception as loop_err:
                logger.exception(f"Processing loop recovered from error: {loop_err}")
                await asyncio.sleep(1)  # Brief pause before continuing

    async def _execute_toolbelt(self, toolbelt: List[Dict[str, Any]]):
        """The Unified Action Executor.
        
        Performs the script from the Brain by executing each action in sequence.
        This is where the AI's decisions are translated into real actions.
        
        Args:
            toolbelt: List of actions to execute, each with tool_name and parameters
        """
        logger.info(f"Executing toolbelt with {len(toolbelt)} actions")
        
        for i, action in enumerate(toolbelt):
            tool_name = action.get("tool_name")
            parameters = action.get("parameters", {})
            
            logger.info(f"Executing action {i+1}/{len(toolbelt)}: {tool_name}")

            try:
                if tool_name == "set_ui_state":
                    # Change the UI state (e.g., switch to drawing mode)
                    await self._frontend_client.set_ui_state(
                        self._room, self.caller_identity, parameters
                    )
                
                elif tool_name == "speak":
                    # Use LiveKit's TTS with interruption support
                    text = parameters.get("text", "")
                    if text and self.agent_session:
                        await self.agent_session.say(text=text, allow_interruptions=True)
                        logger.info(f"Spoke: {text[:50]}...")
                
                elif tool_name in ["draw", "browser_navigate", "browser_click", "browser_type"]:
                    # Execute visual actions on the frontend
                    await self._frontend_client.execute_visual_action(
                        self._room, self.caller_identity, tool_name, parameters
                    )
                
                elif tool_name == "highlight_element":
                    # Highlight a specific element on the frontend
                    element_id = parameters.get("element_id")
                    if element_id:
                        await self._frontend_client.highlight_element(
                            self._room, self.caller_identity, element_id
                        )
                
                elif tool_name == "listen":
                    # Set expectation state to wait for interruptions
                    self._expected_user_input_type = "INTERRUPTION"
                    logger.info("State of Expectation set to: INTERRUPTION")
                
                elif tool_name == "prompt_for_student_action":
                    # Prompt student and wait for specific submission
                    prompt_text = parameters.get("prompt_text", "")
                    if prompt_text and self.agent_session:
                        await self.agent_session.say(text=prompt_text, allow_interruptions=True)
                    
                    self._expected_user_input_type = "SUBMISSION"
                    logger.info("State of Expectation set to: SUBMISSION")
                    
                    # The AI's turn is over - wait for student submission
                    break
                
                elif tool_name == "show_feedback":
                    # Show feedback message on the frontend
                    await self._frontend_client.show_feedback(
                        self._room, 
                        self.caller_identity,
                        parameters.get("feedback_type", "info"),
                        parameters.get("message", ""),
                        parameters.get("duration_ms", 3000)
                    )
                
                else:
                    logger.warning(f"Unknown tool_name: {tool_name}")
                    
            except Exception as action_err:
                logger.error(f"Error executing action '{tool_name}': {action_err}", exc_info=True)
                # Continue with next action rather than failing entire toolbelt

    async def speak_text(self, text: str):
        """Convenience method for speaking text via the agent session."""
        if self.agent_session:
            await self.agent_session.say(text=text, allow_interruptions=True)
        else:
            logger.warning("Cannot speak: agent_session not available")

    async def handle_speak_then_listen(self, parameters: dict, caller_identity: str):
        """Legacy method for backward compatibility."""
        text = parameters.get("text", "")
        if text:
            await self.speak_text(text)
        self._expected_user_input_type = "INTERRUPTION"

    async def trigger_langgraph_task(self, task_name: str, json_payload: str, caller_identity: str):
        """Queue a task for processing by the Brain.
        
        Args:
            task_name: Name of the task to execute
            json_payload: JSON string containing task parameters
            caller_identity: Identity of the client making the request
        """
        try:
            payload_data = json.loads(json_payload) if json_payload else {}
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON payload: {e}")
            payload_data = {}
        
        task = {
            "task_name": task_name,
            "caller_identity": caller_identity,
            **payload_data
        }
        
        await self._processing_queue.put(task)
        logger.info(f"Queued task '{task_name}' for Brain processing")


async def entrypoint(ctx: JobContext):
    """Main entrypoint for the Conductor agent.
    
    Sets up the LiveKit agent with the new Conductor architecture:
    - Creates RoxAgent instance with state management
    - Sets up AgentSession with VAD/STT/TTS (no LLM)
    - Registers specialized RPC handlers
    - Starts processing loop and agent session
    """
    logger.info("Starting Rox Conductor entrypoint")
    
    # Create the Conductor instance
    rox_agent_instance = RoxAgent()
    ctx.rox_agent = rox_agent_instance  # Make agent findable by RPC service
    rox_agent_instance._room = ctx.room
    
    # Create AgentSession with audio capabilities only (no LLM)
    # The Conductor will handle all conversation logic
    main_agent_session = agents.AgentSession(
        stt=deepgram.STT(model="nova-2", language="en-US", api_key=os.environ.get("DEEPGRAM_API_KEY")),
        tts=deepgram.TTS(model="aura-2-helena-en", api_key=os.environ.get("DEEPGRAM_API_KEY")),
        vad=silero.VAD.load(),
        turn_detection=MultilingualModel(),
    )
    rox_agent_instance.agent_session = main_agent_session

    # Debug: Log user state transitions for VAD/turn-detection debugging
    def _log_user_state(ev):
        logger.debug("AgentSession user_state changed: %s -> %s", ev.old_state, ev.new_state)
    
    main_agent_session.on("user_state_changed", _log_user_state)
    
    # --- Register RPC Handlers (The Conductor's "Ears") ---
    agent_rpc_service = AgentInteractionService(ctx=ctx)
    service_name = "rox.interaction.AgentInteraction"
    local_participant = ctx.room.local_participant
    
    logger.info("Registering specialized RPC handlers...")
    try:
        # Register the new specialized handlers
        local_participant.register_rpc_method(
            f"{service_name}/student_wants_to_interrupt", 
            agent_rpc_service.student_wants_to_interrupt
        )
        local_participant.register_rpc_method(
            f"{service_name}/student_spoke_or_acted", 
            agent_rpc_service.student_spoke_or_acted
        )
        local_participant.register_rpc_method(
            f"{service_name}/TestPing", 
            agent_rpc_service.TestPing
        )
        
        logger.info("All RPC handlers registered successfully")

    except Exception as e:
        logger.error(f"Failed to register RPC handlers: {e}", exc_info=True)
        return  # Cannot continue without RPC handlers

    # --- Send Agent Ready Handshake ---
    try:
        # Wait briefly for participants to join
        await asyncio.sleep(1)
        
        if len(ctx.room.remote_participants) > 0:
            first_participant_identity = list(ctx.room.remote_participants.keys())[0]
            rox_agent_instance.caller_identity = first_participant_identity
            
            logger.info(f"First participant joined: {first_participant_identity}. Sending 'agent_ready' handshake.")
            
            handshake_payload = json.dumps({
                "type": "agent_ready",
                "agent_identity": ctx.room.local_participant.identity,
            })
            
            await ctx.room.local_participant.publish_data(
                payload=handshake_payload,
                destination_identities=[first_participant_identity],
            )
            logger.info(f"Sent 'agent_ready' to {first_participant_identity}")
        else:
            logger.warning("No participants in room after 1s, skipping initial handshake")

    except Exception as e:
        logger.error(f"Failed to send 'agent_ready' handshake: {e}", exc_info=True)

    logger.info("Conductor fully operational. Starting processing loop and agent session...")
    
    # Start the processing loop as a background task
    processing_task = asyncio.create_task(rox_agent_instance.processing_loop())

    # Start the main agent session for VAD/STT/TTS capabilities
    await main_agent_session.start(
        room=ctx.room,
        agent=rox_agent_instance,
    )
    
    # Keep the agent alive by waiting for the processing task
    await processing_task


if __name__ == "__main__":
    # The livekit.agents.cli framework handles all argument parsing.
    # The 'connect', '--room', '--url', '--api-key', etc. arguments
    # are all parsed automatically by the line below.
    
    # The 'entrypoint' function will be called with a JobContext
    # that is already configured with the room and connection details.
    
    try:
        agents.cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
    except Exception as e:
        # This can help catch fundamental startup errors
        logger.error(f"Failed to start LiveKit Conductor CLI: {e}", exc_info=True)

if __name__ == "__main__":
    # The livekit.agents.cli framework handles all argument parsing.
    # We no longer need any custom argparse logic here.
    # The 'connect', '--room', '--url', '--api-key', etc. arguments
    # are all parsed automatically by the line below.
    
    # The 'entrypoint' function will be called with a JobContext
    # that is already configured with the room and connection details.
    
    try:
        agents.cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
    except Exception as e:
        # This can help catch fundamental startup errors
        logger.error(f"Failed to start LiveKit Agent CLI: {e}", exc_info=True)