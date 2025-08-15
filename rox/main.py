# livekit-service/rox/main.py
#!/usr/bin/env python3
"""
Rox Assistant LiveKit Conductor - Unified Service

This module implements both:
1. The Conductor pattern for sophisticated real-time AI tutoring (LiveKit Agent)
2. A FastAPI service for launching agents via HTTP endpoints

The Conductor orchestrates communication between the Brain (LangGraph), Body (UI actions),
and maintains the State of Expectation for intelligent interaction handling.
"""

import os
import sys
import logging
import asyncio
import json
import uuid
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any, List

# Add project root to path for clean imports
project_root = Path(__file__).resolve().parent
sys.path.insert(0, str(project_root))

from dotenv import load_dotenv

# FastAPI imports for HTTP service mode
from fastapi import FastAPI, HTTPException
try:
    from model import AgentRequest
except ImportError:
    # Define AgentRequest locally if model.py doesn't exist
    from pydantic import BaseModel
    class AgentRequest(BaseModel):
        room_name: str
        room_url: str

# LiveKit imports
from livekit import rtc, agents
from livekit.agents import Agent, JobContext, WorkerOptions
from livekit.agents.llm import LLM, ChatChunk, ChoiceDelta, ChatContext
from contextlib import asynccontextmanager
from livekit.plugins import deepgram, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

# Local application imports
from generated.protos import interaction_pb2
from rpc_services import AgentInteractionService
from langgraph_client import LangGraphClient
from frontend_client import FrontendClient
# from gemini_tts_client import GeminiTTSClient
from utils.ui_action_factory import build_ui_action_request

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
    class TranscriptInterceptor(LLM):
        """LLM shim that intercepts user speech, enqueues a LangGraph task, and yields no TTS."""
        def __init__(self, outer: "RoxAgent", debounce_ms: int = 500):
            super().__init__()
            self._outer = outer
            self._debounce_ms = debounce_ms
            self._buffer: list[str] = []
            self._debounce_handle: Optional[asyncio.TimerHandle] = None

        def chat(self, *, chat_ctx: ChatContext = None, tools=None, tool_choice=None, **kwargs):  # noqa: D401
            logger.info(f"TranscriptInterceptor.chat called with chat_ctx: {chat_ctx}, kwargs: {kwargs}")
            return self._chat_ctx_mgr(chat_ctx)

        @asynccontextmanager
        async def _chat_ctx_mgr(self, chat_ctx: ChatContext):  # noqa: D401
            logger.info(f"TranscriptInterceptor._chat_ctx_mgr called with chat_ctx: {chat_ctx}")
            # Extract latest user transcript
            transcript = ""
            if chat_ctx:
                logger.info(f"Chat context has items: {getattr(chat_ctx, '_items', [])}")
                messages = getattr(chat_ctx, "_items", [])
                for msg in reversed(messages):
                    role = getattr(msg, "role", None) or (msg.get("role") if isinstance(msg, dict) else None)
                    logger.info(f"Processing message with role: {role}, content: {getattr(msg, 'content', None)}")
                    if str(role).lower() == "user":
                        transcript = getattr(msg, "content", None) or (
                            msg.get("content") if isinstance(msg, dict) else None
                        )
                        if isinstance(transcript, list):
                            transcript = " ".join(map(str, transcript))
                        transcript = str(transcript)
                        logger.info(f"Found user transcript: {transcript}")
                        break
            else:
                logger.warning("No chat_ctx provided to TranscriptInterceptor")
                
            if transcript:
                logger.info(f"Adding transcript to buffer: {transcript}")
                self._buffer.append(transcript)
                # reset debounce timer
                if self._debounce_handle:
                    self._debounce_handle.cancel()
                loop = asyncio.get_event_loop()
                self._debounce_handle = loop.call_later(self._debounce_ms / 1000.0, lambda: asyncio.create_task(self._flush()))
                logger.info(f"Set debounce timer for {self._debounce_ms}ms")
            else:
                logger.warning("No transcript found in chat context")
                
            # Yield a single empty chunk so that AgentSession does not trigger TTS.
            try:
                yield self._stream_empty()
            finally:
                pass

        async def _flush(self):
            logger.info(f"TranscriptInterceptor._flush called with buffer: {self._buffer}")
            full_transcript = " ".join(self._buffer)
            self._buffer.clear()
            logger.info(f"Intercepted transcript: {full_transcript}")

            # Create task payload for LangGraph
            task_name = "student_spoke_or_acted"
            turn_payload = {
                "transcript": full_transcript,
                "current_context": {
                    "user_id": self._outer.user_id,
                    "session_id": self._outer.session_id,
                    "interaction_type": "speech"
                }
            }
            
            logger.info(f"=== SENDING TO LANGGRAPH ===")
            logger.info(f"Task Name: {task_name}")
            logger.info(f"Payload: {json.dumps(turn_payload, indent=2)}")
            logger.info(f"Caller Identity: {self._outer.caller_identity}")
            logger.info(f"User ID: {self._outer.user_id}")
            logger.info(f"Session ID: {self._outer.session_id}")
            logger.info(f"==============================")
            
            # Forward transcript to LangGraph via the conductor's task queue
            await self._outer.trigger_langgraph_task(
                task_name=task_name,
                json_payload=json.dumps(turn_payload),
                caller_identity=self._outer.caller_identity
            )

        async def _stream_empty(self):
            yield ChatChunk(id=str(uuid.uuid4()), delta=ChoiceDelta(role="assistant", content=""))
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
        self.curriculum_id: str = "ai_business_expert_424d7f"  # Default curriculum, can be set dynamically
        self.current_lo_id: Optional[str] = None  # Current learning objective ID
        
        # State of Expectation - determines how to interpret student input
        self._expected_user_input_type: str = "INTERRUPTION"  # Default state
        
        # Interruption handling
        self._interruption_pending: bool = False
        self._current_execution_cancelled: bool = False

        # Plan pause/resume state
        self._current_delivery_plan: Optional[List[Dict]] = None
        self._current_plan_index: int = 0
        
        # Interruption context storage for resumption
        self._interrupted_plan: Optional[List[Dict]] = None
        self._interrupted_plan_index: int = 0
        self._interrupted_plan_context: Optional[Dict] = None
        self._interruption_timestamp: Optional[float] = None
        self._is_paused: bool = False
        
        # Processing queue for tasks from the Brain
        self._processing_queue: asyncio.Queue = asyncio.Queue()
        
        # --- LLM Interceptor for Speech Processing ---
        self.llm_interceptor = self.TranscriptInterceptor(self)
        
        # --- Communication Clients ---
        # LangGraph client for brain communication
        self._langgraph_client = LangGraphClient()
        # Frontend client enabled for visual actions
        self._frontend_client = FrontendClient()
        # Keep other clients disabled for now
        self._gemini_tts_client = None
        logger.info("LangGraph client initialized - RPC forwarding enabled")
        
        # LiveKit components
        self._room: Optional[rtc.Room] = None
        self.agent_session: Optional[agents.AgentSession] = None
        
        logger.info("RoxAgent Conductor initialized")

    async def processing_loop(self):
        """The main engine of the Conductor.
        
        This loop continuously processes tasks from the queue:
        1. Receives task from RPC handlers or TranscriptInterceptor
        2. Adds session context (user_id, curriculum_id, lo_id) to the task
        3. Sends task to Brain (LangGraph) for processing
        4. Parses the returned state to update its own session context (the new lo_id)
        5. Executes the returned action script (the delivery_plan)
        """
        logger.info("Conductor's main processing loop started")
        
        while True:
            try:
                # Wait for a task to be queued (from user speech or RPC)
                task = await self._processing_queue.get()
                logger.info(f"DEQUEUED TASK: {json.dumps(task, indent=2)}")

                # --- Handle special local tasks that don't need LangGraph ---
                task_name = task.get('task_name')
                
                if task_name == "student_stopped_listening":
                    # Handle manual mic-off: process any pending transcript
                    logger.info("[MANUAL_STOP] Processing manual mic turn-off")
                    await self._process_manual_stop_listening(task)
                    self._processing_queue.task_done()
                    continue

                # --- Add current_lo_id to the task payload for the client ---
                # This ensures the client always sends the most current topic
                task["current_lo_id"] = self.current_lo_id

                # 1. Ask the Brain for the next state and actions
                logger.info(f"Forwarding task to LangGraph: {task.get('task_name')}")
                
                # --- Call the updated client signature ---
                response = await self._langgraph_client.invoke_langgraph_task(task, self.user_id, self.curriculum_id, self.session_id)
                
                if not response:
                    logger.error("Received empty response from Brain. Skipping turn.")
                    self._processing_queue.task_done()
                    continue

                # --- Parse the full state dictionary, not just a toolbelt ---
                logger.info(f"RECEIVED FINAL STATE FROM BRAIN: {json.dumps(response, indent=2)}")

                # 2. Update the Conductor's own state from the Brain's response
                new_lo_id = response.get("current_lo_id")
                if new_lo_id and new_lo_id != self.current_lo_id:
                    self.current_lo_id = new_lo_id
                    logger.info(f"SESSION CONTEXT UPDATED. New current_lo_id is: {self.current_lo_id}")

                # 3. Extract the action script (delivery_plan) and execute it
                delivery_plan = response.get("delivery_plan", {})
                actions = delivery_plan.get("actions", [])
                
                # Reset cancellation flag AND pause flag before executing interruption response
                # This ensures interruption responses (speak, listen, etc.) always run
                if task.get("interaction_type") == "interruption":
                    self._current_execution_cancelled = False
                    self._is_paused = False  # Reset pause flag BEFORE execution starts
                    logger.info("[INTERRUPTION] Reset cancellation and pause flags - interruption response will execute fully")
                
                if not actions:
                    logger.warning("No actions found in the delivery plan from the Brain.")
                else:
                    await self._execute_toolbelt(actions)

                # --- ENHANCED RESUMPTION LOGIC ---
                # Handle different meta actions from the Brain after interruption
                meta_action = delivery_plan.get("meta_action") if isinstance(delivery_plan, dict) else None

                if task.get("interaction_type") == "interruption":
                    # Pause flag was already reset before execution - handle meta actions
                    if meta_action == "RESUME":
                        logger.info("[META] Brain requested RESUME - continuing from interruption point")
                        await self._resume_interrupted_plan()
                    elif meta_action == "DISCARD":
                        logger.info("[META] Brain instructed to discard original plan.")
                        self._clear_interrupted_plan_context()
                    else:
                        logger.info("[META] Interruption handled. Original plan context preserved for potential resumption.")
                # --- END ENHANCED RESUMPTION LOGIC ---
                
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
        
        # Pre-process to combine consecutive speak actions for smoother TTS
        optimized_toolbelt = self._optimize_speech_actions(toolbelt)
        # Store current plan for potential pause/resume
        self._current_delivery_plan = list(optimized_toolbelt)

        for i, action in enumerate(optimized_toolbelt):
            # Update current index
            self._current_plan_index = i
            # Respect pause flag
            if self._is_paused:
                logger.info(f"Execution paused before action {i+1}/{len(optimized_toolbelt)}")
                return
            # Check for interruption before each action
            if self._current_execution_cancelled:
                logger.info(f"Execution cancelled due to interruption. Stopping at action {i+1}/{len(toolbelt)}")
                self._current_execution_cancelled = False  # Reset for next execution
                return
            
            tool_name = action.get("tool_name")
            parameters = action.get("parameters", {})
            
            logger.info(f"Executing action {i+1}/{len(toolbelt)}: {tool_name}")

            try:
                if tool_name == "set_ui_state":
                    # Change the UI state (e.g., switch to drawing mode)
                    if self._frontend_client:
                        await self._frontend_client.set_ui_state(
                            self._room, self.caller_identity, parameters
                        )
                    else:
                        logger.info(f"Frontend client not available - would set UI state: {parameters}")
                
                elif tool_name == "speak" or tool_name == "speak_text":
                    text = parameters.get("text", "")
                    if text and self.agent_session:
                        try:
                            # Clean the text to prevent TTS artifacts
                            cleaned_text = text.strip()
                            if not cleaned_text:
                                logger.warning("Empty text after cleaning, skipping TTS")
                                continue
                            
                            # 1. Get the handle for the playback with improved settings
                            logger.info(f"Starting TTS for: {cleaned_text[:50]}...")
                            
                            # Add small delay to prevent connection issues
                            await asyncio.sleep(0.1)
                            
                            playback_handle = await self.agent_session.say(
                                cleaned_text, 
                                allow_interruptions=True
                            )
                            logger.info(f"TTS started successfully: {cleaned_text[:50]}...")
                            
                            # 2. Wait for this specific playback to complete with shorter timeout
                            # Use the correct LiveKit API - await the handle directly
                            await asyncio.wait_for(playback_handle, timeout=20.0)
                            logger.info("TTS completed successfully.")
                            
                            # Add small delay after completion to prevent audio artifacts
                            await asyncio.sleep(0.2)
                            
                        except asyncio.TimeoutError:
                            logger.error(f"TTS timeout after 20s for text: {cleaned_text[:50]}...")
                            # Try to interrupt the playback handle to prevent artifacts
                            try:
                                if 'playback_handle' in locals():
                                    playback_handle.interrupt()
                            except:
                                pass
                        except Exception as tts_error:
                            logger.error(f"TTS error for text '{cleaned_text[:50]}...': {tts_error}")
                            # Continue execution instead of failing completely
                    else:
                        logger.warning("No text to speak or agent_session not available")
                
                elif tool_name in ["draw", "browser_navigate", "browser_click", "browser_type"]:
                    # Execute visual actions on the frontend
                    if self._frontend_client:
                        await self._frontend_client.execute_visual_action(
                            self._room, self.caller_identity, tool_name, parameters
                        )
                    else:
                        logger.warning(f"Frontend client not available - would execute {tool_name} with parameters: {parameters}")
                
                # --- Advanced Jupyter Notebook Actions (Script Player Support) ---
                elif tool_name == "jupyter_type_in_cell":
                    # Type code into a specific Jupyter cell
                    if self._frontend_client:
                        await self._frontend_client.execute_visual_action(
                            self._room, self.caller_identity, tool_name, parameters
                        )
                    else:
                        logger.warning(f"Frontend client not available - would execute {tool_name} with parameters: {parameters}")
                
                elif tool_name == "jupyter_run_cell":
                    # Run a specific Jupyter cell
                    if self._frontend_client:
                        await self._frontend_client.execute_visual_action(
                            self._room, self.caller_identity, tool_name, parameters
                        )
                    else:
                        logger.warning(f"Frontend client not available - would execute {tool_name} with parameters: {parameters}")
                
                elif tool_name == "jupyter_create_new_cell":
                    # Create a new Jupyter cell
                    if self._frontend_client:
                        await self._frontend_client.execute_visual_action(
                            self._room, self.caller_identity, tool_name, parameters
                        )
                    else:
                        logger.warning(f"Frontend client not available - would execute {tool_name} with parameters: {parameters}")
                
                elif tool_name == "jupyter_scroll_to_cell":
                    # Scroll to a specific Jupyter cell
                    if self._frontend_client:
                        await self._frontend_client.execute_visual_action(
                            self._room, self.caller_identity, tool_name, parameters
                        )
                    else:
                        logger.warning(f"Frontend client not available - would execute {tool_name} with parameters: {parameters}")
                
                elif tool_name == "jupyter_click_pyodide":
                    # Click on Pyodide kernel option in Jupyter
                    if self._frontend_client:
                        await self._frontend_client.execute_visual_action(
                            self._room, self.caller_identity, tool_name, parameters
                        )
                    else:
                        logger.warning(f"Frontend client not available - would execute {tool_name} with parameters: {parameters}")
                
                elif tool_name == "highlight_cell_for_doubt_resolution":
                    # Highlight a specific Jupyter cell for doubt resolution
                    if self._frontend_client:
                        await self._frontend_client.execute_visual_action(
                            self._room, self.caller_identity, tool_name, parameters
                        )
                    else:
                        logger.warning(f"Frontend client not available - would execute {tool_name} with parameters: {parameters}")
                
                elif tool_name == "clear_all_annotations":
                    # Clear all visual annotations
                    if self._frontend_client:
                        await self._frontend_client.clear_all_annotations(self._room, self.caller_identity)
                        logger.info("Cleared all annotations")
                    else:
                        logger.warning("Frontend client not available - would clear all annotations")
                
                # --- NEW FRONTEND VOCABULARY TOOLS ---
                elif tool_name == 'generate_visualization':
                    # Generate professional visualization on canvas -> DISPLAY_VISUAL_AID
                    if self._frontend_client:
                        # Support both new structured format (elements) and legacy format (prompt)
                        elements = parameters.get('elements')
                        prompt = parameters.get('prompt')
                        
                        await self._frontend_client.generate_visualization(
                            self._room, self.caller_identity, 
                            elements=elements,
                            prompt=prompt
                        )
                        
                        if elements:
                            logger.info(f"Generated visualization with {len(elements)} structured elements")
                        else:
                            logger.info(f"Generated visualization: {prompt}")
                    else:
                        logger.warning(f"Frontend client not available - would generate visualization: {parameters}")
                
                elif tool_name == 'highlight_elements':
                    # Highlight specific UI elements -> HIGHLIGHT_TEXT_RANGES
                    if self._frontend_client:
                        await self._frontend_client.highlight_elements(
                            self._room, self.caller_identity,
                            element_ids=parameters.get('element_ids', []),
                            highlight_type=parameters.get('highlight_type', 'attention'),
                            duration_ms=parameters.get('duration_ms', 3000)
                        )
                        logger.info(f"Highlighted elements: {parameters.get('element_ids', [])}")
                    else:
                        logger.warning(f"Frontend client not available - would highlight elements: {parameters.get('element_ids', [])}")
                
                elif tool_name == 'give_student_control':
                    # Transfer control to student with message
                    if self._frontend_client:
                        await self._frontend_client.give_student_control(
                            self._room, self.caller_identity,
                            message=parameters.get('message', '')
                        )
                        logger.info(f"Gave student control: {parameters.get('message', '')}")
                    else:
                        logger.warning(f"Frontend client not available - would give student control: {parameters.get('message', '')}")
                
                elif tool_name == 'take_ai_control':
                    # AI regains control with message
                    if self._frontend_client:
                        await self._frontend_client.take_ai_control(
                            self._room, self.caller_identity,
                            message=parameters.get('message', '')
                        )
                        logger.info(f"AI took control: {parameters.get('message', '')}")
                    else:
                        logger.warning(f"Frontend client not available - would take AI control: {parameters.get('message', '')}")
                
                elif tool_name == 'show_feedback':
                    # Show feedback message to student
                    if self._frontend_client:
                        await self._frontend_client.show_feedback(
                            self._room, self.caller_identity,
                            message=parameters.get('message', ''),
                            feedback_type=parameters.get('type', 'info'),
                            duration_ms=parameters.get('duration_ms', 5000)
                        )
                        logger.info(f"Showed feedback: {parameters.get('message', '')}")
                    else:
                        logger.warning(f"Frontend client not available - would show feedback: {parameters.get('message', '')}")
                
                elif tool_name == "listen":
                    # Set expectation state to wait for interruptions
                    self._expected_user_input_type = "INTERRUPTION"
                    logger.info("State of Expectation set to: INTERRUPTION")
                    
                    # Also enable microphone in frontend to allow student to speak
                    if self._frontend_client:
                        await self._frontend_client.set_mic_enabled(
                            self._room, self.caller_identity, True,
                            message="You may speak now..."
                        )
                        logger.info("[LISTEN] Enabled microphone for student input")
                    else:
                        logger.warning("[LISTEN] Frontend client not available - microphone not enabled")
                
                elif tool_name == "START_LISTENING_VISUAL":
                    # Enable microphone via frontend RPC
                    if self._frontend_client:
                        logger.info(f"[START_LISTENING_VISUAL] Attempting to enable mic for caller_identity: {self.caller_identity}")
                        success = await self._frontend_client.set_mic_enabled(
                            self._room, self.caller_identity, True,
                            message=parameters.get('message', '')
                        )
                        if success:
                            logger.info(f"[START_LISTENING_VISUAL] Successfully enabled microphone: {parameters.get('message', '')}")
                        else:
                            logger.error(f"[START_LISTENING_VISUAL] Failed to enable microphone via RPC. caller_identity: {self.caller_identity}, room: {self._room is not None}")
                    else:
                        logger.warning(f"[START_LISTENING_VISUAL] Frontend client not available - would enable mic: {parameters.get('message', '')}")
                
                elif tool_name == "STOP_LISTENING_VISUAL":
                    # Disable microphone via frontend RPC
                    if self._frontend_client:
                        await self._frontend_client.set_mic_enabled(
                            self._room, self.caller_identity, False,
                            message=parameters.get('message', '')
                        )
                        logger.info(f"Disabled microphone: {parameters.get('message', '')}")
                    else:
                        logger.warning(f"Frontend client not available - would disable mic: {parameters.get('message', '')}")
                
                elif tool_name == "prompt_for_student_action":
                    # Prompt student and wait for specific submission
                    prompt_text = parameters.get("prompt_text", "")
                    if prompt_text and self.agent_session:
                        await self.agent_session.say(text=prompt_text, allow_interruptions=True)
                    
                    self._expected_user_input_type = "SUBMISSION"
                    logger.info("State of Expectation set to: SUBMISSION")
                    
                    # The AI's turn is over - wait for student submission
                    break
                
                # --- EXCALIDRAW CANVAS ACTIONS ---
                elif tool_name == 'clear_canvas':
                    # Clear all elements from Excalidraw canvas
                    if self._frontend_client:
                        await self._frontend_client.execute_visual_action(
                            self._room, self.caller_identity, tool_name, parameters
                        )
                        logger.info("Cleared Excalidraw canvas")
                    else:
                        logger.warning("Frontend client not available - would clear canvas")

                elif tool_name == 'update_elements':
                    # Update existing elements on canvas
                    if self._frontend_client:
                        await self._frontend_client.execute_visual_action(
                            self._room, self.caller_identity, tool_name, parameters
                        )
                        logger.info(f"Updated canvas elements: {len(parameters.get('elements', []))} elements")
                    else:
                        logger.warning(f"Frontend client not available - would update elements: {parameters}")

                elif tool_name == 'remove_highlighting':
                    # Remove all highlighting from canvas
                    if self._frontend_client:
                        await self._frontend_client.execute_visual_action(
                            self._room, self.caller_identity, tool_name, parameters
                        )
                        logger.info("Removed canvas highlighting")
                    else:
                        logger.warning("Frontend client not available - would remove highlighting")

                elif tool_name == 'highlight_elements_advanced':
                    # Advanced highlighting with custom options
                    if self._frontend_client:
                        await self._frontend_client.execute_visual_action(
                            self._room, self.caller_identity, tool_name, parameters
                        )
                        logger.info(f"Advanced highlighting: {parameters}")
                    else:
                        logger.warning(f"Frontend client not available - would do advanced highlighting: {parameters}")

                elif tool_name == 'modify_elements':
                    # Modify existing canvas elements
                    if self._frontend_client:
                        await self._frontend_client.execute_visual_action(
                            self._room, self.caller_identity, tool_name, parameters
                        )
                        logger.info(f"Modified canvas elements: {len(parameters.get('modifications', []))} modifications")
                    else:
                        logger.warning(f"Frontend client not available - would modify elements: {parameters}")

                elif tool_name == 'capture_screenshot':
                    # Capture canvas screenshot
                    if self._frontend_client:
                        result = await self._frontend_client.execute_visual_action(
                            self._room, self.caller_identity, tool_name, parameters
                        )
                        logger.info("Captured canvas screenshot")
                        return result
                    else:
                        logger.warning("Frontend client not available - would capture screenshot")
                        return None

                elif tool_name == 'get_canvas_elements':
                    # Get current canvas elements
                    if self._frontend_client:
                        result = await self._frontend_client.execute_visual_action(
                            self._room, self.caller_identity, tool_name, parameters
                        )
                        logger.info("Retrieved canvas elements")
                        return result
                    else:
                        logger.warning("Frontend client not available - would get canvas elements")
                        return []

                elif tool_name == 'set_generating':
                    # Set generating state for UI
                    if self._frontend_client:
                        await self._frontend_client.execute_visual_action(
                            self._room, self.caller_identity, tool_name, parameters
                        )
                        logger.info(f"Set generating state: {parameters.get('generating', False)}")
                    else:
                        logger.warning(f"Frontend client not available - would set generating: {parameters}")

                elif tool_name == 'clear_all_annotations':
                    # Clear all visual annotations and highlights
                    if self._frontend_client:
                        await self._frontend_client.clear_all_annotations(
                            self._room, self.caller_identity
                        )
                        logger.info("Cleared all canvas annotations")
                    else:
                        logger.warning("Frontend client not available - would clear all annotations")
                
                elif tool_name == 'capture_canvas_screenshot':
                    # Capture screenshot of current canvas state
                    if self._frontend_client:
                        # This would need to be implemented in frontend_client.py
                        logger.info("Canvas screenshot capture requested (implementation needed)")
                        # TODO: Implement screenshot capture in frontend_client
                    else:
                        logger.warning("Frontend client not available - would capture canvas screenshot")
                
                elif tool_name == 'get_canvas_elements':
                    # Get list of all elements currently on canvas
                    if self._frontend_client:
                        # This would need to be implemented in frontend_client.py
                        logger.info("Canvas elements list requested (implementation needed)")
                        # TODO: Implement get_canvas_elements in frontend_client
                    else:
                        logger.warning("Frontend client not available - would get canvas elements")
                
                else:
                    logger.warning(f"Unknown tool_name: {tool_name}")
                    
            except Exception as action_err:
                logger.error(f"Error executing action '{tool_name}': {action_err}", exc_info=True)
                # Continue with next action rather than failing entire toolbelt

    def _optimize_speech_actions(self, toolbelt: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Combine consecutive speak actions to prevent TTS hiccups.
        
        This method merges multiple consecutive 'speak' actions into single calls
        with natural pauses, eliminating audio gaps and creating smooth playback.
        """
        if not toolbelt:
            return toolbelt
            
        optimized = []
        current_speech_buffer = []
        
        for action in toolbelt:
            tool_name = action.get("tool_name")
            
            if tool_name in ["speak", "speak_text"]:
                # Accumulate consecutive speak actions
                text = action.get("parameters", {}).get("text", "")
                if text.strip():
                    current_speech_buffer.append(text.strip())
            else:
                # Flush accumulated speech before non-speech action
                if current_speech_buffer:
                    combined_text = " ".join(current_speech_buffer)
                    optimized.append({
                        "tool_name": "speak",
                        "parameters": {"text": combined_text}
                    })
                    current_speech_buffer = []
                
                # Add the non-speech action
                optimized.append(action)
        
        # Flush any remaining speech at the end
        if current_speech_buffer:
            combined_text = " ".join(current_speech_buffer)
            optimized.append({
                "tool_name": "speak",
                "parameters": {"text": combined_text}
            })
        
        logger.info(f"Speech optimization: {len(toolbelt)} actions -> {len(optimized)} actions")
        return optimized

    async def speak_text(self, text: str):
        """Convenience method for speaking text via the agent session."""
        if self.agent_session:
            await self.agent_session.say(text, allow_interruptions=False)
        else:
            logger.warning("Cannot speak: agent_session not available")

    async def handle_speak_then_listen(self, parameters: dict, caller_identity: str):
        """Legacy method for backward compatibility."""
        text = parameters.get("text", "")
        if text:
            await self.speak_text(text)
        self._expected_user_input_type = "INTERRUPTION"

    async def handle_interruption(self, task: Dict[str, Any]):
        """Handle interruption by stopping current execution and forwarding to Brain.
        
        Args:
            task: The interruption task dictionary
        """
        logger.info(f"[INTERRUPTION] Handling interruption: {task.get('task_name')}")
        
        # 1. Pause current execution immediately
        self._is_paused = True
        self._current_execution_cancelled = True
        
        # 2. Try to interrupt any ongoing TTS
        if self.agent_session:
            try:
                self.agent_session.interrupt()
                logger.info("[INTERRUPTION] Stopped ongoing TTS")
            except RuntimeError as e:
                logger.warning(f"[INTERRUPTION] Could not interrupt TTS: {e}")
        
        # 3. Clear any pending tasks in the queue (interruption takes priority)
        while not self._processing_queue.empty():
            try:
                discarded_task = self._processing_queue.get_nowait()
                logger.info(f"[INTERRUPTION] Discarded pending task: {discarded_task.get('task_name')}")
                self._processing_queue.task_done()
            except asyncio.QueueEmpty:
                break
        
        # 4. Capture and store complete plan context for resumption
        import time
        last_action = None
        next_action = None
        remaining_actions = []
        
        try:
            if self._current_delivery_plan is not None:
                # Store the complete interrupted plan state
                self._interrupted_plan = self._current_delivery_plan.copy()
                self._interrupted_plan_index = self._current_plan_index
                self._interruption_timestamp = time.time()
                
                # Capture current and next actions
                if 0 <= self._current_plan_index < len(self._current_delivery_plan):
                    last_action = self._current_delivery_plan[self._current_plan_index]
                if 0 <= (self._current_plan_index + 1) < len(self._current_delivery_plan):
                    next_action = self._current_delivery_plan[self._current_plan_index + 1]
                
                # Capture all remaining actions for potential resumption
                if self._current_plan_index < len(self._current_delivery_plan):
                    remaining_actions = self._current_delivery_plan[self._current_plan_index:]
                
                logger.info(f"[INTERRUPTION] Stored plan context: index={self._current_plan_index}, remaining_actions={len(remaining_actions)}")
        except Exception as e:
            logger.warning(f"[INTERRUPTION] Could not capture plan context: {e}")

        interrupted_plan_context = {
            "last_action": last_action, 
            "next_action": next_action,
            "remaining_actions": remaining_actions,
            "interrupted_at_index": self._current_plan_index,
            "total_plan_length": len(self._current_delivery_plan) if self._current_delivery_plan else 0,
            "interruption_timestamp": self._interruption_timestamp
        }
        
        # Store context for potential resumption
        self._interrupted_plan_context = interrupted_plan_context

        # 5. Immediately forward interruption to Brain with priority, including context
        logger.info(f"[INTERRUPTION] Forwarding to Brain: {task.get('task_name')}")
        enriched_task = {
            **task,
            "interaction_type": "interruption",
            "interrupted_plan_context": interrupted_plan_context,
        }
        await self._processing_queue.put(enriched_task)

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

    async def _handle_vad_mic_off(self):
        """Handle automatic mic turn-off detected by VAD.
        
        This method is called when VAD detects the user has stopped speaking.
        It immediately disables the microphone via frontend RPC.
        """
        logger.info("[VAD_MIC_OFF] Auto-disabling microphone after speech detection")
        
        # Disable mic via frontend RPC
        if self._frontend_client and self.caller_identity:
            success = await self._frontend_client.set_mic_enabled(
                room=self._room,
                identity=self.caller_identity,
                enabled=False,
                message="Processing your input..."
            )
            if success:
                logger.info("[VAD_MIC_OFF] Successfully disabled microphone via frontend RPC")
            else:
                logger.warning("[VAD_MIC_OFF] Failed to disable microphone via frontend RPC")
        else:
            logger.warning("[VAD_MIC_OFF] Frontend client or caller_identity not available for mic disable")

    async def _process_manual_stop_listening(self, task: Dict[str, Any]):
        """Process manual mic turn-off by user.
        
        Args:
            task: The student_stopped_listening task
        """
        logger.info("[MANUAL_STOP] User manually turned off microphone")
        
        # Get any pending transcript from the buffer
        transcript = ""
        if (hasattr(self.llm_interceptor, '_buffer') and 
            self.llm_interceptor._buffer):
            transcript = " ".join(self.llm_interceptor._buffer)
            self.llm_interceptor._buffer.clear()
            logger.info(f"[MANUAL_STOP] Retrieved transcript from buffer: {transcript}")
        
        # If there's transcript content, process it
        if transcript.strip():
            # Queue the transcript for LangGraph processing
            response_task = {
                "task_name": "handle_response",
                "caller_identity": task.get("caller_identity"),
                "transcript": transcript,
                "interaction_type": "manual_stop_speech"
            }
            await self._processing_queue.put(response_task)
            logger.info("[MANUAL_STOP] Queued transcript for Brain processing")
        else:
            logger.info("[MANUAL_STOP] No transcript to process - mic turned off without speech")

    async def _resume_interrupted_plan(self):
        """Resume execution from the interruption point.
        
        This method continues the original plan from where it was interrupted,
        allowing for seamless continuation after handling a doubt or question.
        """
        if not self._interrupted_plan or self._interrupted_plan_context is None:
            logger.warning("[RESUME] No interrupted plan context available for resumption")
            return
        
        logger.info(f"[RESUME] Resuming interrupted plan from index {self._interrupted_plan_index}")
        logger.info(f"[RESUME] Original plan had {len(self._interrupted_plan)} actions, {len(self._interrupted_plan) - self._interrupted_plan_index} remaining")
        
        # Calculate remaining actions from interruption point
        remaining_actions = self._interrupted_plan[self._interrupted_plan_index:]
        
        if remaining_actions:
            # Restore the plan state and continue execution
            self._current_delivery_plan = remaining_actions
            self._current_plan_index = 0  # Start from beginning of remaining actions
            
            logger.info(f"[RESUME] Executing {len(remaining_actions)} remaining actions")
            await self._execute_toolbelt(remaining_actions)
            
            # Clear the interrupted plan context after successful resumption
            self._clear_interrupted_plan_context()
            logger.info("[RESUME] Successfully resumed and completed interrupted plan")
        else:
            logger.info("[RESUME] No remaining actions to execute - plan was already complete")
            self._clear_interrupted_plan_context()

    def _clear_interrupted_plan_context(self):
        """Clear stored interruption context when no longer needed."""
        self._interrupted_plan = None
        self._interrupted_plan_index = 0
        self._interrupted_plan_context = None
        self._interruption_timestamp = None
        logger.info("[RESUME] Cleared interrupted plan context")

    def get_interruption_context(self) -> Optional[Dict]:
        """Get the current interruption context for external access.
        
        Returns:
            Dictionary containing interruption context or None if no interruption stored
        """
        return self._interrupted_plan_context


async def entrypoint(ctx: JobContext):
    """Main entrypoint for the Conductor agent.
    
    Sets up the LiveKit agent with the new Conductor architecture:
    - Creates RoxAgent instance with state management
    - Sets up AgentSession with VAD/STT/TTS (no LLM)
    - Registers specialized RPC handlers
    - Starts processing loop and agent session
    """
    logger.info("Starting Rox Conductor entrypoint")
    
    # Connect to the LiveKit room
    try:
        await ctx.connect()
        logger.info(f"Successfully connected to LiveKit room '{ctx.room.name}'")
    except Exception as e:
        logger.error(f"Failed to connect to LiveKit room: {e}", exc_info=True)
        return
    
    # Create the Conductor instance
    rox_agent_instance = RoxAgent()
    ctx.rox_agent = rox_agent_instance  # Make agent findable by RPC service
    rox_agent_instance._room = ctx.room
    
    # --- NEW: Read metadata from the token and populate agent state ---
    try:
        local_participant = ctx.room.local_participant
        metadata_str = local_participant.metadata
        metadata = json.loads(metadata_str) if metadata_str else {}
        
        # *** THE KEY CHANGE: Read the curriculum_id from environment metadata ***
        # The metadata is now passed from the worker environment
        metadata_str = os.getenv("STUDENT_TOKEN_METADATA", "{}")
        metadata = json.loads(metadata_str) if metadata_str else {}
        
        rox_agent_instance.user_id = metadata.get("user_id")
        
        # *** THE KEY CHANGE: Read the curriculum_id ***
        rox_agent_instance.curriculum_id = metadata.get("curriculum_id") # Using 'curriculum_id' variable name
        
        # Validate that we have the necessary IDs
        if not rox_agent_instance.user_id or not rox_agent_instance.curriculum_id:
            logger.error(f"CRITICAL: Missing user_id or curriculum_id in metadata. Agent cannot function.")
            return

        rox_agent_instance.current_lo_id = metadata.get("current_lo_id")
        
        logger.info(f"Agent state populated: user_id={rox_agent_instance.user_id}, curriculum_id={rox_agent_instance.curriculum_id}")
    except Exception as e:
        logger.warning(f"Could not parse token metadata, using defaults: {e}")
    
    # Create AgentSession with audio capabilities and LLM interceptor
    # The LLM interceptor will process user speech and forward to LangGraph
    main_agent_session = agents.AgentSession(
        stt=deepgram.STT(
            model="nova-2", 
            language="en", 
            api_key=os.environ.get("DEEPGRAM_API_KEY"),
            # Add real-time STT settings for better capture
            interim_results=True,
            punctuate=True,
            smart_format=True
        ),
        llm=rox_agent_instance.llm_interceptor,
        tts=deepgram.TTS(
            model="aura-asteria-en", 
            api_key=os.environ.get("DEEPGRAM_API_KEY"),
            # Only use supported parameters for connection stability
            encoding="linear16",
            sample_rate=24000
        ),
        vad=silero.VAD.load(),
        turn_detection=MultilingualModel(),
    )
    rox_agent_instance.agent_session = main_agent_session

    # Enhanced user state handler for VAD-based automatic mic turn-off
    def _handle_user_state_change(ev):
        logger.info(f"[VAD] User state changed: {ev.old_state} -> {ev.new_state}")
        
        # When user stops speaking (speaking -> listening), automatically turn off mic
        if (ev.old_state == "speaking" and 
            ev.new_state == "listening"):
            
            logger.info("[VAD] User stopped speaking - auto-disabling mic and processing input")
            
            # ALWAYS turn off the mic when user stops speaking (regardless of transcript)
            asyncio.create_task(rox_agent_instance._handle_vad_mic_off())
            
            # Get the latest transcript from the TranscriptInterceptor buffer (if available)
            transcript = ""
            if (hasattr(rox_agent_instance.llm_interceptor, '_buffer') and 
                rox_agent_instance.llm_interceptor._buffer):
                
                # Get the most recent transcript from the buffer
                transcript = " ".join(rox_agent_instance.llm_interceptor._buffer)
                logger.info(f"[VAD] Found transcript: {transcript}")
                
                # Clear the buffer since we're processing it
                rox_agent_instance.llm_interceptor._buffer.clear()
            else:
                logger.info("[VAD] No transcript available - mic will still be disabled")
            
            # Process speech if we have transcript content
            if transcript.strip():
                asyncio.create_task(rox_agent_instance._process_user_speech_auto(transcript))
            else:
                logger.info("[VAD] No speech content to process, but mic disabled successfully")
    
    # Register the VAD handler AFTER rox_agent_instance is created
    main_agent_session.on("user_state_changed", _handle_user_state_change)
    
    async def _process_user_speech_auto(self, transcript: str):
        """Process user speech automatically detected by VAD.
        
        Args:
            transcript: The speech transcript to process
        """
        logger.info(f"[VAD_AUTO] Processing auto-detected speech: {transcript}")
        
        # Disable mic via frontend RPC
        if self._frontend_client and self.caller_identity:
            await self._frontend_client.set_mic_enabled(
                room=self._room,
                caller_identity=self.caller_identity,
                enabled=False,
                action_type="STOP_LISTENING_VISUAL",
                message="Processing your input..."
            )
            logger.info("[VAD_AUTO] Disabled microphone via frontend RPC")
        
        # Queue the transcript for LangGraph processing
        task = {
            "task_name": "handle_response",
            "caller_identity": self.caller_identity,
            "transcript": transcript,
            "interaction_type": "vad_auto_speech"
        }
        await self._processing_queue.put(task)
        logger.info("[VAD_AUTO] Queued speech transcript for Brain processing")
    
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
            f"{service_name}/student_mic_button_interrupt", 
            agent_rpc_service.student_mic_button_interrupt
        )
        local_participant.register_rpc_method(
            f"{service_name}/student_spoke_or_acted", 
            agent_rpc_service.student_spoke_or_acted
        )
        local_participant.register_rpc_method(
            f"{service_name}/student_stopped_listening", 
            agent_rpc_service.student_stopped_listening
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
    
    # --- THIS IS THE NEW PROACTIVE START LOGIC ---
    logger.info("Agent is live. Checking for student to initiate conversation.")
    try:
        # Give a moment for the frontend participant to be fully connected.
        await asyncio.sleep(2)
        
        if len(ctx.room.remote_participants) > 0:
            # We already have the student's identity from the handshake check
            if not rox_agent_instance.caller_identity:
                 rox_agent_instance.caller_identity = list(ctx.room.remote_participants.keys())[0]

            logger.info(f"Student '{rox_agent_instance.caller_identity}' is present. Queueing proactive start task.")
            
            # Create a special, predefined task.
            # CRITICAL: It has NO 'transcript' because the student hasn't spoken.
            initial_task = {
                "task_name": "start_tutoring_session",
                "caller_identity": rox_agent_instance.caller_identity
            }
            
            # Queue this initial task. The processing_loop will pick it up.
            await rox_agent_instance._processing_queue.put(initial_task)
            
        else:
            logger.warning("No student in the room. Agent will wait for a participant to join.")

    except Exception as e:
        logger.error(f"Failed during proactive start sequence: {e}", exc_info=True)
    
    # Keep the agent alive by waiting for the processing task
    await processing_task


# FastAPI application instance for HTTP service mode
app = FastAPI(title="Rox Agent Service", description="Unified LiveKit Agent and HTTP API")

@app.get("/")
def read_root():
    """Health check endpoint"""
    return {"service": "Rox Agent Service", "status": "running"}

@app.post("/run-agent")
def run_agent(agent_request: AgentRequest):
    """Launch a LiveKit agent for the specified room"""
    room_name = agent_request.room_name
    room_url = agent_request.room_url
    api_key = os.getenv("LIVEKIT_API_KEY")
    api_secret = os.getenv("LIVEKIT_API_SECRET")

    if not all([api_key, api_secret]):
        raise HTTPException(
            status_code=500,
            detail="Server configuration error: LIVEKIT_API_KEY and LIVEKIT_API_SECRET must be set."
        )
    
    python_executable = sys.executable
    current_script = __file__
    print(f"Starting agent for room: {room_name}")
    print(f"Room url: {room_url}")

    try:
        # Launch this same script in agent mode
        command = [
            python_executable,
            current_script,
            "connect",          # The command for the CLI
            "--url",           # The URL flag
            room_url,           # The URL value
            "--room",           # The room flag
            room_name,
            "--api-key",        # The API key flag
            api_key,
            "--api-secret",     # The API secret flag
            api_secret,
        ]
        print(f"Executing command: {' '.join(command)}")

        # Run the agent as a non-blocking subprocess
        process = subprocess.Popen(command)

        return {"message": f"Agent started for room {room_name}", "pid": process.pid}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to run agent: {str(e)}")

def run_fastapi_server():
    """Run the FastAPI server"""
    import uvicorn
    logger.info("Starting FastAPI server on port 5005")
    uvicorn.run(app, host="0.0.0.0", port=5005)


if __name__ == "__main__":
    # Check if we should run in server mode or agent mode
    if len(sys.argv) == 1 or (len(sys.argv) == 2 and sys.argv[1] == "--server"):
        # No arguments or --server flag: run FastAPI server
        run_fastapi_server()
    else:
        # Arguments provided: run as LiveKit agent
        # The livekit.agents.cli framework handles all argument parsing.
        # The 'connect', '--room', '--url', '--api-key', etc. arguments
        # are all parsed automatically by the line below.
        
        # The 'entrypoint' function will be called with a JobContext
        # that is already configured with the room and connection details.
        
        try:
            agents.cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
        except Exception as e:
            # This can help catch fundamental startup errors
            logger.error(f"Failed to start LiveKit Agent CLI: {e}", exc_info=True)
