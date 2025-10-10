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
import time
import contextlib

# Add project root to path for clean imports
project_root = Path(__file__).resolve().parent
sys.path.insert(0, str(project_root))

from dotenv import load_dotenv
from otel_setup import init_tracing
from opentelemetry import trace as otel_trace
from opentelemetry import context as otel_context
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from tracing import ensure_trace_id, set_trace_id, get_trace_id
from logging_setup import install_logging_filter
from pydantic import BaseModel

# FastAPI imports for HTTP service mode
from fastapi import FastAPI, HTTPException, BackgroundTasks

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

# from livekit.plugins.turn_detector.multilingual import MultilingualModel

# Local application imports
from generated.protos import interaction_pb2
from rpc_services import AgentInteractionService
from langgraph_client import LangGraphClient
from frontend_client import FrontendClient
from utils.gcs_signer import generate_v4_signed_url

# from gemini_tts_client import GeminiTTSClient
from utils.ui_action_factory import build_ui_action_request
from browser_pod_client import BrowserPodClient
# Note: heavy turn detection models can trigger a supervised inference subprocess
# which may OOM on constrained containers. We will import lazily only if enabled.

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Debounce window to suppress duplicate mic-enable RPCs (in seconds)
MIC_ENABLE_DEBOUNCE_SEC = float(os.getenv("MIC_ENABLE_DEBOUNCE_SEC", "0.5"))

# --- Environment Loading and Validation ---
load_dotenv(dotenv_path=project_root / ".env")

# Initialize OpenTelemetry tracing for the service (global)
try:
    init_tracing(os.getenv("OTEL_SERVICE_NAME", "livekit-service"))
except Exception:
    pass

# Ensure logs carry rox_trace_id and otel ids
try:
    install_logging_filter()
except Exception:
    pass


# Environment variables validation (skip during testing AND during container startup)
def validate_environment():
    """Validate required environment variables."""
    required_env_vars = ["LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "DEEPGRAM_API_KEY"]

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


# Only validate environment when not in test mode AND when actually running as an agent
def is_running_tests():
    """Check if we're running in a test environment."""
    import inspect

    for frame_info in inspect.stack():
        if "pytest" in frame_info.filename or "test_" in frame_info.filename:
            return True
    return False


def is_running_as_agent():
    """Check if we're running as a LiveKit agent (not just HTTP server)."""
    # Only validate when we're actually connecting to LiveKit
    return len(sys.argv) > 1 and ("connect" in sys.argv or "--room" in sys.argv)


# FIXED: Only validate environment when running as agent, not during container startup
# if not is_running_tests() and is_running_as_agent():
#     logger.info("Running as LiveKit agent - validating environment...")
#     is_valid, message = validate_environment()
#     if not is_valid:
#         logger.error(f"Environment validation failed: {message}")
#         sys.exit(1)
# else:
#     logger.info("Not running as agent or in test mode - skipping environment validation")


class RoxAgent(Agent):
    class TranscriptInterceptor(LLM):
        """LLM shim that intercepts user speech, enqueues a LangGraph task, and yields no TTS."""

        def __init__(self, outer: "RoxAgent", debounce_ms: int = 500):
            super().__init__()
            self._outer = outer
            self._debounce_ms = debounce_ms
            self._buffer: list[str] = []
            self._debounce_handle: Optional[asyncio.TimerHandle] = None

        def chat(
            self,
            *,
            chat_ctx: ChatContext = None,
            tools=None,
            tool_choice=None,
            **kwargs,
        ):  # noqa: D401
            logger.info(
                f"TranscriptInterceptor.chat called with chat_ctx: {chat_ctx}, kwargs: {kwargs}"
            )
            return self._chat_ctx_mgr(chat_ctx)

        @asynccontextmanager
        async def _chat_ctx_mgr(self, chat_ctx: ChatContext):  # noqa: D401
            logger.info(
                f"TranscriptInterceptor._chat_ctx_mgr called with chat_ctx: {chat_ctx}"
            )
            # Extract latest user transcript
            transcript = ""
            if chat_ctx:
                logger.info(
                    f"Chat context has items: {getattr(chat_ctx, '_items', [])}"
                )
                messages = getattr(chat_ctx, "_items", [])
                for msg in reversed(messages):
                    role = getattr(msg, "role", None) or (
                        msg.get("role") if isinstance(msg, dict) else None
                    )
                    logger.info(
                        f"Processing message with role: {role}, content: {getattr(msg, 'content', None)}"
                    )
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
                logger.info("Transcript buffered; VAD handler will flush on turn end")
            else:
                logger.warning("No transcript found in chat context")

            # Yield a single empty chunk so that AgentSession does not trigger TTS.
            try:
                yield self._stream_empty()
            finally:
                pass

        async def _flush(self):
            logger.info(
                f"TranscriptInterceptor._flush called with buffer: {self._buffer}"
            )
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
                    "interaction_type": "speech",
                },
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
                caller_identity=self._outer.caller_identity,
            )

        async def _stream_empty(self):
            yield ChatChunk(
                id=str(uuid.uuid4()), delta=ChoiceDelta(role="assistant", content="")
            )

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
            "You are Rox, an AI tutor. You help students learn through interactive conversations and activities.",
        )
        super().__init__(**kwargs)

        # --- State Management ---
        self.user_id: Optional[str] = None
        self.session_id: Optional[str] = None
        self.caller_identity: Optional[str] = None  # The participant ID of the frontend
        self.curriculum_id: str = (
            "ai_business_expert_424d7f"  # Default curriculum, can be set dynamically
        )
        self.current_lo_id: Optional[str] = None  # Current learning objective ID

        # State of Expectation - determines how to interpret student input
        self._expected_user_input_type: str = "INTERRUPTION"  # Default state

        # Interruption handling
        self._interruption_pending: bool = False
        self._current_execution_cancelled: bool = False
        self._session_started: bool = False

        # Plan pause/resume state
        self._current_delivery_plan: Optional[List[Dict]] = None
        self._current_plan_index: int = 0

        # Interruption context storage for resumption
        self._interrupted_plan: Optional[List[Dict]] = None
        self._interrupted_plan_index: int = 0
        self._interrupted_plan_context: Optional[Dict] = None
        self._interruption_timestamp: Optional[float] = None
        self._is_paused: bool = False
        # Debounce tracking for mic enables
        self._last_mic_enable_ts: Optional[float] = None

        # Processing queue for tasks from the Brain
        self._processing_queue: asyncio.Queue = asyncio.Queue()
        # Pending scheduled task to end a user turn after hangover
        self._pending_turn_end_task: Optional[asyncio.Task] = None

        # --- LLM Interceptor for Speech Processing ---
        self.llm_interceptor = self.TranscriptInterceptor(self)

        # --- Communication Clients ---
        # Buffer for rrweb events captured over the LiveKit data channel
        self.rrweb_events_buffer: List[Dict[str, Any]] = []
        # Buffer for live monitoring events (rrweb or vscode) used by Kamikaze
        self.live_events_buffer: List[Dict[str, Any]] = []
        # LangGraph client for brain communication (pass self for rrweb buffer access)
        self._langgraph_client = LangGraphClient(self)
        # Frontend client enabled for visual actions
        self._frontend_client = FrontendClient()
        # Browser pod client for LiveKit data-channel control
        self._browser_pod_client = BrowserPodClient()
        # Keep other clients disabled for now
        self._gemini_tts_client = None
        logger.info("LangGraph client initialized - RPC forwarding enabled")

        # LiveKit components
        self._room: Optional[rtc.Room] = None
        self.agent_session: Optional[agents.AgentSession] = None
        # One-time guard to avoid sending initial navigate more than once
        self._initial_nav_sent: bool = False
        # Guard to avoid registering the browser join callback multiple times
        self._browser_join_cb_registered: bool = False

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

                # Set per-task trace context for logs and spans if present
                try:
                    tid = task.get("trace_id") if isinstance(task, dict) else None
                    if tid:
                        set_trace_id(tid)
                        span = otel_trace.get_current_span()
                        if span:
                            span.set_attribute("rox.trace_id", tid)
                except Exception:
                    pass

                # --- Handle special local tasks that don't need LangGraph ---
                task_name = task.get("task_name")

                # On session start, proactively instruct the browser pod to navigate to example.com
                if task_name == "start_tutoring_session":
                    try:
                        browser_pod_identity = None
                        if self._room is not None and getattr(self._room, "name", None):
                            browser_pod_identity = f"browser-bot-{self._room.name}"
                        # Fallback to session_id if present
                        if not browser_pod_identity and getattr(
                            self, "session_id", None
                        ):
                            browser_pod_identity = f"browser-bot-{self.session_id}"

                        if (
                            self._browser_pod_client
                            and self._room
                            and browser_pod_identity
                        ):
                            # Wait briefly for the browser pod to join the room before sending the command
                            wait_sec = float(os.getenv("BROWSER_JOIN_WAIT_SEC", "6"))
                            interval = 0.2
                            attempts = int(max(1, wait_sec / interval))
                            seen = False
                            try:
                                for _ in range(attempts):
                                    rp = getattr(self._room, "remote_participants", {})
                                    if (
                                        isinstance(rp, dict)
                                        and browser_pod_identity in rp
                                    ):
                                        seen = True
                                        break
                                    await asyncio.sleep(interval)
                            except Exception:
                                # Non-fatal; proceed best-effort
                                pass

                            if not seen:
                                logger.warning(
                                    f"[AUTO] Browser pod '{browser_pod_identity}' not present after {wait_sec:.1f}s; skipping initial navigate"
                                )
                                # Fallback: watch for late join and send once when present
                                max_watch_sec = float(
                                    os.getenv("BROWSER_JOIN_WATCH_SEC", "60")
                                )

                                async def _watch_and_send():
                                    try:
                                        steps = int(max(1, max_watch_sec / 0.5))
                                        for _ in range(steps):
                                            if self._initial_nav_sent:
                                                return
                                            rp2 = getattr(
                                                self._room, "remote_participants", {}
                                            )
                                            if (
                                                isinstance(rp2, dict)
                                                and browser_pod_identity in rp2
                                            ):
                                                try:
                                                    await self._browser_pod_client.send_browser_command(
                                                        self._room,
                                                        browser_pod_identity,
                                                        "browser_navigate",

                                                    )
                                                    self._initial_nav_sent = True
                                                    logger.info(
                                                        f"[AUTO][watch] Sent initial navigate to example.com -> {browser_pod_identity}"
                                                    )
                                                except Exception as e:
                                                    logger.error(
                                                        f"[AUTO][watch] Failed to send initial navigate: {e}"
                                                    )
                                                return
                                            await asyncio.sleep(0.5)
                                        logger.warning(
                                            f"[AUTO][watch] Browser pod '{browser_pod_identity}' did not join within {max_watch_sec:.1f}s"
                                        )
                                    except Exception:
                                        logger.debug(
                                            "[AUTO][watch] error while waiting for browser join",
                                            exc_info=True,
                                        )

                                asyncio.create_task(_watch_and_send())
                            else:
                                await self._browser_pod_client.send_browser_command(
                                    self._room,
                                    browser_pod_identity,
                                    "browser_navigate"
                                  
                                )
                                logger.info(
                                    f"[AUTO] Sent initial navigate to example.com -> {browser_pod_identity}"
                                )
                                self._initial_nav_sent = True
                        else:
                            logger.warning(
                                "[AUTO] Cannot send initial navigate: room or browser identity not available"
                            )
                    except Exception as e:
                        logger.error(f"[AUTO] Failed to send initial navigate: {e}")

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
                response = await self._langgraph_client.invoke_langgraph_task(
                    task, self.user_id, self.curriculum_id, self.session_id
                )

                if not response:
                    logger.error("Received empty response from Brain. Skipping turn.")
                    self._processing_queue.task_done()
                    continue

                # --- Parse the full state dictionary, not just a toolbelt ---
                logger.info(
                    f"RECEIVED FINAL STATE FROM BRAIN: {json.dumps(response, indent=2)}"
                )

                # 2. Update the Conductor's own state from the Brain's response
                new_lo_id = response.get("current_lo_id")
                if new_lo_id and new_lo_id != self.current_lo_id:
                    self.current_lo_id = new_lo_id
                    logger.info(
                        f"SESSION CONTEXT UPDATED. New current_lo_id is: {self.current_lo_id}"
                    )

                # Persisting session state to Redis is disabled to keep livekit-service stateless

                # 3. Extract the action script (delivery_plan) and execute it
                delivery_plan = response.get("delivery_plan", {})
                actions = delivery_plan.get("actions", [])
                # Also read optional metadata for UI hints (e.g., suggested responses)
                metadata = (
                    delivery_plan.get("metadata", {})
                    if isinstance(delivery_plan, dict)
                    else {}
                )

                # Proactively emit Suggested Responses if provided only in metadata
                try:
                    if metadata:
                        meta_suggestions = (
                            metadata.get("suggested_responses")
                            or metadata.get("suggestions")
                            or metadata.get("responses")
                        )
                        meta_title = metadata.get("title") or metadata.get("prompt")

                        if meta_suggestions:
                            try:
                                count = (
                                    len(meta_suggestions)
                                    if isinstance(meta_suggestions, list)
                                    else 0
                                )
                            except Exception:
                                count = 0
                            logger.info(
                                f"[META] Found suggested responses in delivery_plan.metadata: count={count}, title={meta_title!r}"
                            )

                            if self._frontend_client:
                                try:
                                    # Accept both rich objects and plain strings
                                    if isinstance(meta_suggestions, list) and all(
                                        isinstance(x, str) for x in meta_suggestions
                                    ):
                                        ok = await self._frontend_client.send_suggested_responses(
                                            self._room,
                                            self.caller_identity,
                                            responses=meta_suggestions,
                                            title=meta_title,
                                        )
                                    else:
                                        ok = await self._frontend_client.send_suggested_responses(
                                            self._room,
                                            self.caller_identity,
                                            suggestions=meta_suggestions,
                                            title=meta_title,
                                        )

                                    if ok:
                                        logger.info(
                                            f"[META] Dispatched SUGGESTED_RESPONSES to frontend successfully (count={count})"
                                        )
                                    else:
                                        logger.error(
                                            "[META] Frontend RPC returned failure for SUGGESTED_RESPONSES from metadata"
                                        )
                                except Exception as e:
                                    logger.error(
                                        f"[META] Failed sending SUGGESTED_RESPONSES from metadata: {e}",
                                        exc_info=True,
                                    )
                            else:
                                logger.warning(
                                    "[META] Frontend client not available - would send SUGGESTED_RESPONSES from metadata"
                                )
                        else:
                            logger.debug(
                                "[META] No suggested responses present in delivery_plan.metadata"
                            )
                except Exception:
                    logger.debug(
                        "[META] Error while inspecting/sending metadata-based suggested responses",
                        exc_info=True,
                    )

                # Reset cancellation flag AND pause flag before executing interruption response
                # This ensures interruption responses (speak, listen, etc.) always run
                if task.get("interaction_type") == "interruption":
                    self._current_execution_cancelled = False
                    self._is_paused = False  # Reset pause flag BEFORE execution starts
                    logger.info(
                        "[INTERRUPTION] Reset cancellation and pause flags - interruption response will execute fully"
                    )

                if not actions:
                    logger.warning(
                        "No actions found in the delivery plan from the Brain."
                    )
                else:
                    await self._execute_toolbelt(actions)

                # --- ENHANCED RESUMPTION LOGIC ---
                # Handle different meta actions from the Brain after interruption
                meta_action = (
                    delivery_plan.get("meta_action")
                    if isinstance(delivery_plan, dict)
                    else None
                )

                if task.get("interaction_type") == "interruption":
                    # Pause flag was already reset before execution - handle meta actions
                    if meta_action == "RESUME":
                        logger.info(
                            "[META] Brain requested RESUME - continuing from interruption point"
                        )
                        await self._resume_interrupted_plan()
                    elif meta_action == "DISCARD":
                        logger.info("[META] Brain instructed to discard original plan.")
                        self._clear_interrupted_plan_context()
                    else:
                        logger.info(
                            "[META] Interruption handled. Original plan context preserved for potential resumption."
                        )
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
                logger.info(
                    f"Execution paused before action {i+1}/{len(optimized_toolbelt)}"
                )
                return
            # Check for interruption before each action
            if self._current_execution_cancelled:
                logger.info(
                    f"Execution cancelled due to interruption. Stopping at action {i+1}/{len(toolbelt)}"
                )
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
                        logger.info(
                            f"Frontend client not available - would set UI state: {parameters}"
                        )

                elif tool_name == "trigger_rrweb_replay":
                    # Trigger rrweb replay on the frontend via RPC.
                    # Transform GCS URLs to use the one-backend proxy endpoint for persistent access.
                    raw_url = str(parameters.get("events_url", "") or "")
                    asset_id = None
                    
                    try:
                        # Extract asset_id from various URL formats
                        if raw_url.startswith("gs://"):
                            # gs://bucket/path/to/file.json -> path/to/file.json
                            without = raw_url[5:]
                            parts = without.split("/", 1)
                            if len(parts) == 2:
                                asset_id = parts[1]  # Skip bucket name
                        elif "storage.googleapis.com/" in raw_url:
                            # https://storage.googleapis.com/bucket/path/to/file.json -> path/to/file.json
                            try:
                                after = raw_url.split("storage.googleapis.com/", 1)[1]
                                parts = after.split("/", 1)
                                if len(parts) == 2:
                                    asset_id = parts[1]  # Skip bucket name
                            except Exception:
                                pass
                        else:
                            # Assume it's already just the asset_id
                            asset_id = raw_url
                        
                        # Construct proxy URL through one-backend
                        backend_url = os.getenv("ONE_BACKEND_URL", "http://localhost:3001")
                        proxy_url = f"{backend_url.rstrip('/')}/api/assets/rrweb/{asset_id}"
                        logger.info(f"Transformed rrweb URL to proxy: {raw_url} -> {proxy_url}")
                        
                    except Exception as e:
                        logger.warning(f"Failed to transform rrweb URL, using raw: {e}")
                        proxy_url = raw_url

                    if self._frontend_client:
                        ok = await self._frontend_client.trigger_rrweb_replay(self._room, self.caller_identity, proxy_url)
                        if ok:
                            logger.info("Dispatched RRWEB_REPLAY to frontend")
                        else:
                            logger.error("Frontend RPC returned failure for RRWEB_REPLAY")
                    else:
                        logger.warning("Frontend client not available - would trigger rrweb replay")

                elif tool_name == "speak" or tool_name == "speak_text":
                    text = parameters.get("text", "")
                    if text and self.agent_session:
                        try:
                            # Clean the text to prevent TTS artifacts
                            cleaned_text = text.strip()
                            if not cleaned_text:
                                logger.warning(
                                    "Empty text after cleaning, skipping TTS"
                                )
                                continue

                            # 1. Get the handle for the playback with improved settings
                            logger.info(f"Starting TTS for: {cleaned_text[:50]}...")

                            # Add small delay to prevent connection issues
                            await asyncio.sleep(0.1)

                            # Ensure any prior playback (e.g., ack) is stopped before new TTS
                            try:
                                self.agent_session.interrupt()
                            except RuntimeError:
                                pass

                            playback_handle = await self.agent_session.say(
                                cleaned_text, allow_interruptions=True
                            )
                            logger.info(
                                f"TTS started successfully: {cleaned_text[:50]}..."
                            )

                            # 2. Wait for this specific playback to complete with shorter timeout
                            # Use the correct LiveKit API - await the handle directly
                            await asyncio.wait_for(playback_handle, timeout=20.0)
                            logger.info("TTS completed successfully.")

                            # Add small delay after completion to prevent audio artifacts
                            await asyncio.sleep(0.2)

                        except asyncio.TimeoutError:
                            logger.error(
                                f"TTS timeout after 20s for text: {cleaned_text[:50]}..."
                            )
                            # Try to interrupt the playback handle to prevent artifacts
                            try:
                                if "playback_handle" in locals():
                                    playback_handle.interrupt()
                            except:
                                pass
                        except Exception as tts_error:
                            logger.error(
                                f"TTS error for text '{cleaned_text[:50]}...': {tts_error}"
                            )
                            # Continue execution instead of failing completely
                    else:
                        logger.warning(
                            "No text to speak or agent_session not available"
                        )

                elif tool_name == "wait":
                    # Pause execution for a specified number of milliseconds
                    ms = parameters.get("ms") or parameters.get("milliseconds") or 0
                    try:
                        ms_val = float(ms)
                    except Exception:
                        ms_val = 0.0
                    # Clamp to a sane maximum (10 minutes)
                    ms_val = max(0.0, min(ms_val, 600000.0))
                    logger.info(f"[WAIT] Sleeping for {ms_val} ms before next action...")
                    await asyncio.sleep(ms_val / 1000.0)

                elif tool_name == "draw":
                    # Execute drawing on the frontend
                    if self._frontend_client:
                        await self._frontend_client.execute_visual_action(
                            self._room, self.caller_identity, tool_name, parameters
                        )
                    else:
                        logger.warning(
                            f"Frontend client not available - would execute {tool_name} with parameters: {parameters}"
                        )

                elif tool_name in [
                    "browser_navigate",
                    "browser_click",
                    "browser_type",
                    "jupyter_type_in_cell",
                    "jupyter_run_cell",
                    "setup_jupyter",
                    "vscode_add_line",
                    "vscode_delete_line",
                    "vscode_run_terminal_command",
                    "vscode_highlight_add",
                    "vscode_highlight_remove",
                    "vscode_create_file", # Create a file in the vscode workspace
                ]:
                    # Send commands to the browser pod over LiveKit data channel
                    browser_pod_identity = (
                        f"browser-bot-{self.session_id}" if self.session_id else None
                    )
                    if self._browser_pod_client and self._room and browser_pod_identity:
                        try:
                            await self._browser_pod_client.send_browser_command(
                                self._room,
                                browser_pod_identity,
                                tool_name,
                                parameters,
                            )
                            logger.info(
                                f"Sent {tool_name} to browser pod {browser_pod_identity}"
                            )
                        except Exception as e:
                            logger.error(
                                f"Failed to send {tool_name} to browser pod: {e}"
                            )
                    else:
                        logger.warning(
                            "Browser Pod client or room/session_id not available; cannot send browser action"
                        )

                # --- Advanced Jupyter Notebook Actions (Script Player Support) ---
                elif tool_name == "jupyter_type_in_cell":
                    # Type code into a specific Jupyter cell
                    if self._frontend_client:
                        await self._frontend_client.execute_visual_action(
                            self._room, self.caller_identity, tool_name, parameters
                        )
                    else:
                        logger.warning(
                            f"Frontend client not available - would execute {tool_name} with parameters: {parameters}"
                        )

                elif tool_name == "jupyter_run_cell":
                    # Run a specific Jupyter cell
                    if self._frontend_client:
                        await self._frontend_client.execute_visual_action(
                            self._room, self.caller_identity, tool_name, parameters
                        )
                    else:
                        logger.warning(
                            f"Frontend client not available - would execute {tool_name} with parameters: {parameters}"
                        )

                elif tool_name == "jupyter_create_new_cell":
                    # Create a new Jupyter cell
                    if self._frontend_client:
                        await self._frontend_client.execute_visual_action(
                            self._room, self.caller_identity, tool_name, parameters
                        )
                    else:
                        logger.warning(
                            f"Frontend client not available - would execute {tool_name} with parameters: {parameters}"
                        )

                elif tool_name == "jupyter_scroll_to_cell":
                    # Scroll to a specific Jupyter cell
                    if self._frontend_client:
                        await self._frontend_client.execute_visual_action(
                            self._room, self.caller_identity, tool_name, parameters
                        )
                    else:
                        logger.warning(
                            f"Frontend client not available - would execute {tool_name} with parameters: {parameters}"
                        )

                elif tool_name == "jupyter_click_pyodide":
                    # Click on Pyodide kernel option in Jupyter
                    if self._frontend_client:
                        await self._frontend_client.execute_visual_action(
                            self._room, self.caller_identity, tool_name, parameters
                        )
                    else:
                        logger.warning(
                            f"Frontend client not available - would execute {tool_name} with parameters: {parameters}"
                        )

                elif tool_name == "setup_jupyter":
                    # Setup Jupyter environment (navigate, select kernel, upload files)
                    if self._frontend_client:
                        await self._frontend_client.execute_visual_action(
                            self._room, self.caller_identity, tool_name, parameters
                        )
                    else:
                        logger.warning(
                            f"Frontend client not available - would execute {tool_name} with parameters: {parameters}"
                        )

                elif tool_name == "highlight_cell_for_doubt_resolution":
                    # Highlight a specific Jupyter cell for doubt resolution
                    if self._frontend_client:
                        await self._frontend_client.execute_visual_action(
                            self._room, self.caller_identity, tool_name, parameters
                        )
                    else:
                        logger.warning(
                            f"Frontend client not available - would execute {tool_name} with parameters: {parameters}"
                        )

                elif tool_name == "clear_all_annotations":
                    # Clear all visual annotations
                    if self._frontend_client:
                        await self._frontend_client.clear_all_annotations(
                            self._room, self.caller_identity
                        )
                        logger.info("Cleared all annotations")
                    else:
                        logger.warning(
                            "Frontend client not available - would clear all annotations"
                        )

                # --- NEW FRONTEND VOCABULARY TOOLS ---
                elif tool_name == "display_visual_aid":
                    # Failsafe: validate/sanitize params before forwarding to frontend/Visualizer
                    if self._frontend_client:
                        try:
                            raw_params = parameters or {}
                            topic_context = str(
                                raw_params.get("topic_context", "") or ""
                            ).strip()

                            # Accept either 'text_to_visualize' or legacy 'prompt'
                            raw_prompt = raw_params.get("text_to_visualize")
                            if raw_prompt is None:
                                raw_prompt = raw_params.get("prompt")

                            # Normalize prompt to string
                            if raw_prompt is None:
                                prompt = ""
                            elif isinstance(raw_prompt, (dict, list)):
                                # Defensive: stringify complex objects
                                prompt = json.dumps(raw_prompt, ensure_ascii=False)
                            else:
                                prompt = str(raw_prompt)

                            prompt = prompt.strip()

                            # Basic semantic validation: must contain at least one alphanumeric
                            def _has_meaningful_text(s: str) -> bool:
                                return any(ch.isalnum() for ch in s)

                            if not prompt or not _has_meaningful_text(prompt):
                                warn_msg = "Unable to generate a diagram: empty or non-meaningful prompt."
                                logger.warning(
                                    f"[display_visual_aid] {warn_msg} params={raw_params}"
                                )
                                # Notify user on frontend and skip
                                await self._frontend_client.show_feedback(
                                    self._room,
                                    self.caller_identity,
                                    feedback_type="error",
                                    message=warn_msg,
                                    duration_ms=5000,
                                )
                                continue

                            # Truncate overly long prompts to protect downstream services
                            MAX_PROMPT_LEN = 8000
                            if len(prompt) > MAX_PROMPT_LEN:
                                logger.info(
                                    f"[display_visual_aid] Truncating prompt from {len(prompt)} to {MAX_PROMPT_LEN} chars"
                                )
                                prompt = prompt[:MAX_PROMPT_LEN]

                            clean_payload = {
                                "prompt": prompt,
                                "topic_context": topic_context,
                            }

                            # Forward to frontend; it will call the Visualizer service
                            ok = await self._frontend_client.execute_visual_action(
                                self._room,
                                self.caller_identity,
                                "GENERATE_VISUALIZATION",
                                clean_payload,
                            )
                            if ok:
                                logger.info(
                                    "[display_visual_aid] Forwarded sanitized prompt to frontend for visualization"
                                )
                            else:
                                logger.error(
                                    "[display_visual_aid] Frontend RPC returned failure for GENERATE_VISUALIZATION"
                                )
                        except Exception as e:
                            logger.error(
                                f"[display_visual_aid] Failed to process/forward visualization request: {e}",
                                exc_info=True,
                            )
                            try:
                                await self._frontend_client.show_feedback(
                                    self._room,
                                    self.caller_identity,
                                    feedback_type="error",
                                    message="Visualization failed due to an internal error.",
                                    duration_ms=5000,
                                )
                            except Exception:
                                pass
                    else:
                        logger.warning(
                            f"Frontend client not available - would forward display_visual_aid: {parameters}"
                        )

                # --- Feed/Whiteboard Block Management ---
                elif tool_name == "add_excalidraw_block":
                    # Normalize parameters for frontend expectations
                    raw = parameters or {}
                    out_params = {}
                    # id
                    if raw.get("id"):
                        out_params["id"] = raw.get("id")
                    elif raw.get("block_id"):
                        out_params["id"] = raw.get("block_id")
                    # summary/title
                    if raw.get("summary") is not None:
                        out_params["summary"] = raw.get("summary")
                    elif raw.get("title") is not None:
                        out_params["summary"] = raw.get("title")
                    # elements list or JSON string
                    elements = raw.get("elements")
                    if elements is None:
                        elements = raw.get("initial_elements")
                    if elements is not None:
                        out_params["elements"] = elements
                    if self._frontend_client and self._room and self.caller_identity:
                        try:
                            ok = await self._frontend_client.execute_visual_action(
                                self._room, self.caller_identity, "add_excalidraw_block", out_params
                            )
                            if ok:
                                logger.info("[ADD_BLOCK] Forwarded add_excalidraw_block to frontend")
                            else:
                                logger.error("[ADD_BLOCK] Frontend RPC returned failure for ADD_EXCALIDRAW_BLOCK")
                        except Exception as e:
                            logger.error(f"[ADD_BLOCK] Failed to forward add_excalidraw_block: {e}", exc_info=True)
                    else:
                        logger.warning(
                            f"Frontend client not available - would add_excalidraw_block: {out_params}"
                        )

                elif tool_name == "update_excalidraw_block":
                    raw = parameters or {}
                    out_params = {}
                    # id / block_id
                    bid = raw.get("id") or raw.get("block_id") or raw.get("blockId")
                    if bid:
                        out_params["id"] = bid
                    # elements: prefer 'elements', else fall back to 'modifications' from mock brain
                    elems = raw.get("elements")
                    if elems is None:
                        elems = raw.get("modifications")
                    if elems is not None:
                        out_params["elements"] = elems
                    if self._frontend_client and self._room and self.caller_identity:
                        try:
                            ok = await self._frontend_client.execute_visual_action(
                                self._room, self.caller_identity, "update_excalidraw_block", out_params
                            )
                            if ok:
                                logger.info("[UPDATE_BLOCK] Forwarded update_excalidraw_block to frontend")
                            else:
                                logger.error("[UPDATE_BLOCK] Frontend RPC returned failure for UPDATE_EXCALIDRAW_BLOCK")
                        except Exception as e:
                            logger.error(f"[UPDATE_BLOCK] Failed to forward update_excalidraw_block: {e}", exc_info=True)
                    else:
                        logger.warning(
                            f"Frontend client not available - would update_excalidraw_block: {out_params}"
                        )

                elif tool_name == "focus_on_block":
                    raw = parameters or {}
                    out_params = {}
                    bid = raw.get("id") or raw.get("block_id") or raw.get("blockId")
                    if bid:
                        out_params["id"] = bid
                    if self._frontend_client and self._room and self.caller_identity:
                        try:
                            ok = await self._frontend_client.execute_visual_action(
                                self._room, self.caller_identity, "focus_on_block", out_params
                            )
                            if ok:
                                logger.info("[FOCUS_BLOCK] Forwarded focus_on_block to frontend")
                            else:
                                logger.error("[FOCUS_BLOCK] Frontend RPC returned failure for FOCUS_ON_BLOCK")
                        except Exception as e:
                            logger.error(f"[FOCUS_BLOCK] Failed to forward focus_on_block: {e}", exc_info=True)
                    else:
                        logger.warning(
                            f"Frontend client not available - would focus_on_block: {out_params}"
                        )

                elif tool_name == "highlight_elements":
                    # Highlight specific UI elements -> HIGHLIGHT_TEXT_RANGES
                    if self._frontend_client:
                        await self._frontend_client.highlight_elements(
                            self._room,
                            self.caller_identity,
                            element_ids=parameters.get("element_ids", []),
                            highlight_type=parameters.get(
                                "highlight_type", "attention"
                            ),
                            duration_ms=parameters.get("duration_ms", 3000),
                        )
                        logger.info(
                            f"Highlighted elements: {parameters.get('element_ids', [])}"
                        )
                    else:
                        logger.warning(
                            f"Frontend client not available - would highlight elements: {parameters.get('element_ids', [])}"
                        )

                elif tool_name == "give_student_control":
                    # Transfer control to student with message
                    if self._frontend_client:
                        await self._frontend_client.give_student_control(
                            self._room,
                            self.caller_identity,
                            message=parameters.get("message", ""),
                        )
                        logger.info(
                            f"Gave student control: {parameters.get('message', '')}"
                        )
                    else:
                        logger.warning(
                            f"Frontend client not available - would give student control: {parameters.get('message', '')}"
                        )

                elif tool_name == "take_ai_control":
                    # AI regains control with message
                    if self._frontend_client:
                        await self._frontend_client.take_ai_control(
                            self._room,
                            self.caller_identity,
                            message=parameters.get("message", ""),
                        )
                        logger.info(f"AI took control: {parameters.get('message', '')}")
                    else:
                        logger.warning(
                            f"Frontend client not available - would take AI control: {parameters.get('message', '')}"
                        )

                elif tool_name == "show_feedback":
                    # Show feedback message to student
                    if self._frontend_client:
                        await self._frontend_client.show_feedback(
                            self._room,
                            self.caller_identity,
                            message=parameters.get("message", ""),
                            feedback_type=parameters.get("type", "info"),
                            duration_ms=parameters.get("duration_ms", 5000),
                        )
                        logger.info(f"Showed feedback: {parameters.get('message', '')}")
                    else:
                        logger.warning(
                            f"Frontend client not available - would show feedback: {parameters.get('message', '')}"
                        )

                elif tool_name in [
                    "suggested_responses",
                    "show_suggested_responses",
                    "SUGGESTED_RESPONSES",
                ]:
                    # Emit quick reply suggestions to the frontend
                    if self._frontend_client:
                        try:
                            ok = await self._frontend_client.send_suggested_responses(
                                self._room,
                                self.caller_identity,
                                suggestions=parameters.get("suggestions"),
                                title=parameters.get("title")
                                or parameters.get("prompt"),
                                group_id=parameters.get("group_id"),
                                responses=parameters.get("responses"),
                            )
                            if ok:
                                logger.info(
                                    f"Sent suggested responses: title={parameters.get('title') or parameters.get('prompt')}, count={len(parameters.get('suggestions') or parameters.get('responses') or [])}"
                                )
                            else:
                                logger.error(
                                    "Frontend RPC returned failure for SUGGESTED_RESPONSES"
                                )
                        except Exception as e:
                            logger.error(
                                f"Failed to send suggested responses: {e}",
                                exc_info=True,
                            )
                    else:
                        logger.warning(
                            "Frontend client not available - would send suggested responses"
                        )

                elif tool_name == "listen":
                    logger.info("Preparing to listen, adding a pre-listen delay...")
                    await asyncio.sleep(0.2)  # 200ms pre-listen buffer
                    # Set expectation state to wait for interruptions
                    self._expected_user_input_type = "INTERRUPTION"
                    logger.info("State of Expectation set to: INTERRUPTION")

                    # Also enable microphone in frontend to allow student to speak
                    if self._frontend_client:
                        now = time.time()
                        if self._last_mic_enable_ts is None or (now - self._last_mic_enable_ts) > MIC_ENABLE_DEBOUNCE_SEC:
                            await self._frontend_client.set_mic_enabled(
                                self._room,
                                self.caller_identity,
                                True,
                                message="You may speak now...",
                            )
                            self._last_mic_enable_ts = now
                            logger.info("[LISTEN] Enabled microphone for student input")
                        else:
                            logger.info(
                                f"[LISTEN] Skipping mic enable due to debounce (dt={(now - self._last_mic_enable_ts):.3f}s)"
                            )
                    else:
                        logger.warning(
                            "[LISTEN] Frontend client not available - microphone not enabled"
                        )

                elif tool_name == "START_LISTENING_VISUAL":
                    # Enable microphone via frontend RPC
                    if self._frontend_client:
                        logger.info(
                            f"[START_LISTENING_VISUAL] Attempting to enable mic for caller_identity: {self.caller_identity}"
                        )
                        now = time.time()
                        if self._last_mic_enable_ts is None or (now - self._last_mic_enable_ts) > MIC_ENABLE_DEBOUNCE_SEC:
                            success = await self._frontend_client.set_mic_enabled(
                                self._room,
                                self.caller_identity,
                                True,
                                message=parameters.get("message", ""),
                            )
                            if success:
                                self._last_mic_enable_ts = now
                                logger.info(
                                    f"[START_LISTENING_VISUAL] Successfully enabled microphone: {parameters.get('message', '')}"
                                )
                            else:
                                logger.error(
                                    f"[START_LISTENING_VISUAL] Failed to enable microphone via RPC. caller_identity: {self.caller_identity}, room: {self._room is not None}"
                                )
                        else:
                            logger.info(
                                f"[START_LISTENING_VISUAL] Skipping mic enable due to debounce (dt={(now - self._last_mic_enable_ts):.3f}s)"
                            )
                    else:
                        logger.warning(
                            f"[START_LISTENING_VISUAL] Frontend client not available - would enable mic: {parameters.get('message', '')}"
                        )

                elif tool_name == "STOP_LISTENING_VISUAL":
                    # Disable microphone via frontend RPC
                    if self._frontend_client:
                        await self._frontend_client.set_mic_enabled(
                            self._room,
                            self.caller_identity,
                            False,
                            message=parameters.get("message", ""),
                        )
                        logger.info(
                            f"Disabled microphone: {parameters.get('message', '')}"
                        )
                    else:
                        logger.warning(
                            f"Frontend client not available - would disable mic: {parameters.get('message', '')}"
                        )

                elif tool_name == "prompt_for_student_action":
                    # Prompt student and wait for specific submission
                    prompt_text = parameters.get("prompt_text", "")
                    if prompt_text and self.agent_session:
                        await self.agent_session.say(
                            text=prompt_text, allow_interruptions=True
                        )

                    self._expected_user_input_type = "SUBMISSION"
                    logger.info("State of Expectation set to: SUBMISSION")

                    # The AI's turn is over - wait for student submission
                    break

                # --- EXCALIDRAW CANVAS ACTIONS ---
                elif tool_name == "clear_canvas":
                    # Clear all elements from Excalidraw canvas
                    if self._frontend_client:
                        await self._frontend_client.execute_visual_action(
                            self._room, self.caller_identity, tool_name, parameters
                        )
                        logger.info("Cleared Excalidraw canvas")
                    else:
                        logger.warning(
                            "Frontend client not available - would clear canvas"
                        )

                elif tool_name == "update_elements":
                    # Update existing elements on canvas
                    if self._frontend_client:
                        await self._frontend_client.execute_visual_action(
                            self._room, self.caller_identity, tool_name, parameters
                        )
                        logger.info(
                            f"Updated canvas elements: {len(parameters.get('elements', []))} elements"
                        )
                    else:
                        logger.warning(
                            f"Frontend client not available - would update elements: {parameters}"
                        )

                elif tool_name == "remove_highlighting":
                    # Remove all highlighting from canvas
                    if self._frontend_client:
                        await self._frontend_client.execute_visual_action(
                            self._room, self.caller_identity, tool_name, parameters
                        )
                        logger.info("Removed canvas highlighting")
                    else:
                        logger.warning(
                            "Frontend client not available - would remove highlighting"
                        )

                elif tool_name == "highlight_elements_advanced":
                    # Advanced highlighting with custom options
                    if self._frontend_client:
                        await self._frontend_client.execute_visual_action(
                            self._room, self.caller_identity, tool_name, parameters
                        )
                        logger.info(f"Advanced highlighting: {parameters}")
                    else:
                        logger.warning(
                            f"Frontend client not available - would do advanced highlighting: {parameters}"
                        )

                elif tool_name == "modify_elements":
                    # Modify existing canvas elements
                    if self._frontend_client:
                        await self._frontend_client.execute_visual_action(
                            self._room, self.caller_identity, tool_name, parameters
                        )
                        logger.info(
                            f"Modified canvas elements: {len(parameters.get('modifications', []))} modifications"
                        )
                    else:
                        logger.warning(
                            f"Frontend client not available - would modify elements: {parameters}"
                        )

                elif tool_name == "capture_screenshot":
                    # Capture canvas screenshot
                    if self._frontend_client:
                        result = await self._frontend_client.execute_visual_action(
                            self._room, self.caller_identity, tool_name, parameters
                        )
                        logger.info("Captured canvas screenshot")
                        return result
                    else:
                        logger.warning(
                            "Frontend client not available - would capture screenshot"
                        )
                        return None

                elif tool_name == "get_canvas_elements":
                    # Get current canvas elements
                    if self._frontend_client:
                        result = await self._frontend_client.execute_visual_action(
                            self._room, self.caller_identity, tool_name, parameters
                        )
                        logger.info("Retrieved canvas elements")
                        return result
                    else:
                        logger.warning(
                            "Frontend client not available - would get canvas elements"
                        )
                        return []

                elif tool_name == "set_generating":
                    # Set generating state for UI
                    if self._frontend_client:
                        await self._frontend_client.execute_visual_action(
                            self._room, self.caller_identity, tool_name, parameters
                        )
                        logger.info(
                            f"Set generating state: {parameters.get('generating', False)}"
                        )
                    else:
                        logger.warning(
                            f"Frontend client not available - would set generating: {parameters}"
                        )

                elif tool_name == "clear_all_annotations":
                    # Clear all visual annotations and highlights
                    if self._frontend_client:
                        await self._frontend_client.clear_all_annotations(
                            self._room, self.caller_identity
                        )
                        logger.info("Cleared all canvas annotations")
                    else:
                        logger.warning(
                            "Frontend client not available - would clear all annotations"
                        )

                elif tool_name == "capture_canvas_screenshot":
                    # Capture screenshot of current canvas state
                    if self._frontend_client:
                        # This would need to be implemented in frontend_client.py
                        logger.info(
                            "Canvas screenshot capture requested (implementation needed)"
                        )
                        # TODO: Implement screenshot capture in frontend_client
                    else:
                        logger.warning(
                            "Frontend client not available - would capture canvas screenshot"
                        )

                elif tool_name == "trigger_rrweb_replay":
                    # Trigger rrweb replay on the frontend given a JSON events URL
                    events_url = parameters.get("events_url")
                    if self._frontend_client and events_url:
                        logger.info(f"Triggering rrweb replay for URL: {events_url}")
                        try:
                            await self._frontend_client.trigger_rrweb_replay(
                                self._room, self.caller_identity, events_url=events_url
                            )
                        except Exception as e:
                            logger.error(f"Failed to trigger rrweb replay: {e}")
                    else:
                        logger.warning(
                            "Frontend client not available or no events_url provided for rrweb replay."
                        )

                elif tool_name == "replay_parsed_rrweb_actions":
                    # NEW: Replay parsed rrweb actions via browser_manager
                    # This queries Brum for parsed actions and sends them to browser_manager
                    curriculum_id = parameters.get("curriculum_id")
                    lo_id = parameters.get("lo_id")
                    delay_ms = parameters.get("delay_ms", 500)
                    
                    if not curriculum_id or not lo_id:
                        logger.warning("Missing curriculum_id or lo_id for replay_parsed_rrweb_actions")
                        continue
                    
                    logger.info(f"Replaying parsed rrweb actions: curriculum={curriculum_id}, lo={lo_id}")
                    
                    try:
                        # Query Brum for parsed actions
                        # TODO: Implement Brum query endpoint for parsed actions
                        # For now, we'll send the query params to browser_manager
                        # and let it handle the Brum query
                        
                        # Find browser-bot participant
                        browser_identity = None
                        for pid, participant in self._room.remote_participants.items():
                            ident = str(getattr(participant, "identity", "") or pid)
                            if ident.startswith("browser-bot-"):
                                browser_identity = ident
                                break
                        
                        if not browser_identity:
                            logger.warning("No browser-bot participant found for replay_parsed_rrweb_actions")
                            continue
                        
                        # Send command to browser_manager via data channel
                        command = {
                            "type": "replay_rrweb_sequence_from_brum",
                            "curriculum_id": curriculum_id,
                            "lo_id": lo_id,
                            "delay_ms": delay_ms
                        }
                        
                        await self._room.local_participant.publish_data(
                            json.dumps(command).encode('utf-8'),
                            destination_identities=[browser_identity]
                        )
                        
                        logger.info(f"Sent replay_parsed_rrweb_actions command to {browser_identity}")
                        
                    except Exception as e:
                        logger.error(f"Failed to replay parsed rrweb actions: {e}", exc_info=True)

                elif tool_name == "get_block_content":
                    # Request content of a whiteboard feed block from the frontend and feed back to LangGraph
                    block_id = str(parameters.get("block_id") or parameters.get("id") or "").strip()
                    if not block_id:
                        logger.warning("[get_block_content] Missing block_id parameter; skipping")
                        continue
                    if self._frontend_client and self._room and self.caller_identity:
                        try:
                            logger.info(f"[get_block_content] Sending RPC to frontend to fetch block content for ID: {block_id}")
                            content = await self._frontend_client.get_block_content_from_frontend(
                                self._room, self.caller_identity, block_id
                            )
                            if content is None:
                                logger.error(f"[get_block_content] Frontend did not return content for ID: {block_id}")
                                continue
                            logger.info(f"[get_block_content] Received block content from frontend for ID: {block_id}")
                            # Enqueue a follow-up task for the Brain with the content
                            followup_task = {
                                "task_name": "handle_response",
                                "caller_identity": self.caller_identity,
                                "interaction_type": "block_content",
                                "block_id": block_id,
                                "block_content": content,
                                # Provide a small transcript marker for logging on the Brain side
                                "transcript": f"[block_content:{block_id}]",
                            }
                            await self._processing_queue.put(followup_task)
                            logger.info(
                                f"[get_block_content] Queued follow-up task with block_id={block_id} for Brain processing"
                            )
                        except Exception as e:
                            logger.error(f"[get_block_content] Error while requesting block content: {e}", exc_info=True)
                    else:
                        logger.warning(
                            "[get_block_content] Frontend client, room, or caller_identity not available; cannot fetch content"
                        )

                elif tool_name == "get_canvas_elements":
                    # Get list of all elements currently on canvas
                    if self._frontend_client:
                        # This would need to be implemented in frontend_client.py
                        logger.info(
                            "Canvas elements list requested (implementation needed)"
                        )
                        # TODO: Implement get_canvas_elements in frontend_client
                    else:
                        logger.warning(
                            "Frontend client not available - would get canvas elements"
                        )

                else:
                    logger.warning(f"Unknown tool_name: {tool_name}")

            except Exception as action_err:
                logger.error(
                    f"Error executing action '{tool_name}': {action_err}", exc_info=True
                )
                # Continue with next action rather than failing entire toolbelt

    def _optimize_speech_actions(
        self, toolbelt: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
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
                    optimized.append(
                        {"tool_name": "speak", "parameters": {"text": combined_text}}
                    )
                    current_speech_buffer = []

                # Add the non-speech action
                optimized.append(action)

        # Flush any remaining speech at the end
        if current_speech_buffer:
            combined_text = " ".join(current_speech_buffer)
            optimized.append(
                {"tool_name": "speak", "parameters": {"text": combined_text}}
            )

        logger.info(
            f"Speech optimization: {len(toolbelt)} actions -> {len(optimized)} actions"
        )
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
                logger.info(
                    f"[INTERRUPTION] Discarded pending task: {discarded_task.get('task_name')}"
                )
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
                if (
                    0
                    <= (self._current_plan_index + 1)
                    < len(self._current_delivery_plan)
                ):
                    next_action = self._current_delivery_plan[
                        self._current_plan_index + 1
                    ]

                # Capture all remaining actions for potential resumption
                if self._current_plan_index < len(self._current_delivery_plan):
                    remaining_actions = self._current_delivery_plan[
                        self._current_plan_index :
                    ]

                logger.info(
                    f"[INTERRUPTION] Stored plan context: index={self._current_plan_index}, remaining_actions={len(remaining_actions)}"
                )
        except Exception as e:
            logger.warning(f"[INTERRUPTION] Could not capture plan context: {e}")

        interrupted_plan_context = {
            "last_action": last_action,
            "next_action": next_action,
            "remaining_actions": remaining_actions,
            "interrupted_at_index": self._current_plan_index,
            "total_plan_length": (
                len(self._current_delivery_plan) if self._current_delivery_plan else 0
            ),
            "interruption_timestamp": self._interruption_timestamp,
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

    async def optimistic_interrupt_ack(self, message: Optional[str] = None):
        """Immediately acknowledge an interruption locally.

        - Interrupt any ongoing TTS and pause current execution
        - Send START_LISTENING_VISUAL to the frontend so the user's mic turns on immediately
        - Set expectation to listen for an interruption

        This is intentionally lightweight and does NOT contact the Brain; callers
        should subsequently invoke `handle_interruption(...)` to forward context to LangGraph.
        """
        try:
            logger.info("[INTERRUPTION][OPTIMISTIC] Acknowledging interrupt locally")

            # Pause/cancel execution immediately (best-effort; full capture happens in handle_interruption)
            self._is_paused = True
            self._current_execution_cancelled = True

            # Stop any ongoing TTS promptly
            if self.agent_session:
                try:
                    self.agent_session.interrupt()
                    logger.info("[INTERRUPTION][OPTIMISTIC] Stopped ongoing TTS")
                except RuntimeError as e:
                    logger.warning(f"[INTERRUPTION][OPTIMISTIC] Could not interrupt TTS: {e}")

            # Speak a quick local acknowledgement to reduce perceived latency
            # Allow interruptions so student speech cuts this off naturally
            try:
                if self.agent_session:
                    ack_text = (message or "Yes — go ahead, what's your doubt?").strip()
                    if ack_text:
                        logger.info(f"[INTERRUPTION][OPTIMISTIC] Speaking quick ack: {ack_text}")
                        playback_handle = await self.agent_session.say(
                            ack_text, allow_interruptions=True
                        )
                        # Do not block; wait briefly in background and time out if needed
                        async def _wait_short_ack():
                            try:
                                await asyncio.wait_for(playback_handle, timeout=2.5)
                            except asyncio.TimeoutError:
                                try:
                                    playback_handle.interrupt()
                                except Exception:
                                    pass
                            except Exception:
                                logger.debug("[INTERRUPTION][OPTIMISTIC] Ack TTS ended with error", exc_info=True)
                        asyncio.create_task(_wait_short_ack())
            except Exception:
                logger.debug("[INTERRUPTION][OPTIMISTIC] Failed to start ack TTS", exc_info=True)

            # Immediately enable the student's microphone on the frontend
            if self._frontend_client and self.caller_identity:
                ok = await self._frontend_client.set_mic_enabled(
                    room=self._room,
                    identity=self.caller_identity,
                    enabled=True,
                    message=message or "You may speak now.",
                )
                if ok:
                    logger.info("[INTERRUPTION][OPTIMISTIC] Dispatched START_LISTENING_VISUAL to frontend")
                    # Optional: show a brief visual prompt on the UI
                    try:
                        await self._frontend_client.show_feedback(
                            room=self._room,
                            identity=self.caller_identity,
                            feedback_type="info",
                            message="Yes—what's your doubt?",
                            duration_ms=1500,
                        )
                    except Exception:
                        logger.debug("[INTERRUPTION][OPTIMISTIC] Failed to show feedback banner", exc_info=True)
                else:
                    logger.warning("[INTERRUPTION][OPTIMISTIC] Frontend rejected START_LISTENING_VISUAL")
            else:
                logger.warning("[INTERRUPTION][OPTIMISTIC] Frontend client or caller_identity missing; cannot enable mic")

            # Update expectation state so subsequent speech is treated as an interruption
            self._expected_user_input_type = "INTERRUPTION"
        except Exception:
            logger.error("[INTERRUPTION][OPTIMISTIC] Failed to acknowledge interrupt locally", exc_info=True)

    async def trigger_langgraph_task(
        self, task_name: str, json_payload: str, caller_identity: str
    ):
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
            **payload_data,
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
                message="Processing your input...",
            )
            if success:
                logger.info(
                    "[VAD_MIC_OFF] Successfully disabled microphone via frontend RPC"
                )
            else:
                logger.warning(
                    "[VAD_MIC_OFF] Failed to disable microphone via frontend RPC"
                )
        else:
            logger.warning(
                "[VAD_MIC_OFF] Frontend client or caller_identity not available for mic disable"
            )

    async def _process_user_speech_auto(self, transcript: str):
        """Process user speech automatically detected by VAD.

        Args:
            transcript: The speech transcript to process
        """
        logger.info(f"[VAD_AUTO] Processing auto-detected speech: {transcript}")

        # Disable mic via frontend RPC (idempotent / safe if already off)
        if self._frontend_client and self.caller_identity:
            try:
                await self._frontend_client.set_mic_enabled(
                    self._room,
                    self.caller_identity,
                    False,
                    message="Processing your input...",
                )
                logger.info("[VAD_AUTO] Disabled microphone via frontend RPC")
            except Exception as e:
                logger.warning(f"[VAD_AUTO] Failed to disable microphone via RPC: {e}")

        # Queue the transcript for LangGraph processing
        task = {
            "task_name": "handle_response",
            "caller_identity": self.caller_identity,
            "transcript": transcript,
            "interaction_type": "vad_auto_speech",
        }
        await self._processing_queue.put(task)
        logger.info("[VAD_AUTO] Queued speech transcript for Brain processing")

    async def _process_manual_stop_listening(self, task: Dict[str, Any]):
        """Process manual mic turn-off by user.

        Args:
            task: The student_stopped_listening task
        """
        logger.info("[MANUAL_STOP] User manually turned off microphone")

        # Get any pending transcript from the buffer
        transcript = ""
        if hasattr(self.llm_interceptor, "_buffer") and self.llm_interceptor._buffer:
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
                "interaction_type": "manual_stop_speech",
            }
            await self._processing_queue.put(response_task)
            logger.info("[MANUAL_STOP] Queued transcript for Brain processing")
        else:
            logger.info(
                "[MANUAL_STOP] No transcript to process - mic turned off without speech"
            )

    async def _resume_interrupted_plan(self):
        """Resume execution from the interruption point.

        This method continues the original plan from where it was interrupted,
        allowing for seamless continuation after handling a doubt or question.
        """
        if not self._interrupted_plan or self._interrupted_plan_context is None:
            logger.warning(
                "[RESUME] No interrupted plan context available for resumption"
            )
            return

        logger.info(
            f"[RESUME] Resuming interrupted plan from index {self._interrupted_plan_index}"
        )
        logger.info(
            f"[RESUME] Original plan had {len(self._interrupted_plan)} actions, {len(self._interrupted_plan) - self._interrupted_plan_index} remaining"
        )

        # Calculate remaining actions from interruption point
        remaining_actions = self._interrupted_plan[self._interrupted_plan_index :]

        if remaining_actions:
            # Restore the plan state and continue execution
            self._current_delivery_plan = remaining_actions
            self._current_plan_index = 0  # Start from beginning of remaining actions

            logger.info(
                f"[RESUME] Executing {len(remaining_actions)} remaining actions"
            )
            await self._execute_toolbelt(remaining_actions)

            # Clear the interrupted plan context after successful resumption
            self._clear_interrupted_plan_context()
            logger.info("[RESUME] Successfully resumed and completed interrupted plan")
        else:
            logger.info(
                "[RESUME] No remaining actions to execute - plan was already complete"
            )
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
    # Create a root span to make trace context active for the whole agent job
    tracer = otel_trace.get_tracer(__name__)
    _root_span = tracer.start_span("livekit.agent.job")
    _root_token = otel_context.attach(otel_trace.set_span_in_context(_root_span))

    logger.info("Starting Rox Conductor entrypoint")
    langgraph_url = os.getenv("LANGGRAPH_TUTOR_URL")
    logger.info(f"LANGGRAPH_TUTOR_URL environment variable: {langgraph_url}")
    # Connect to the LiveKit room
    try:
        await ctx.connect()
        logger.info(f"Successfully connected to LiveKit room '{ctx.room.name}'")
        # Annotate root span with useful room/participant attributes
        try:
            if getattr(ctx, "room", None):
                _root_span.set_attribute("livekit.room.name", getattr(ctx.room, "name", None))
                _root_span.set_attribute("livekit.room.sid", getattr(ctx.room, "sid", None))
                lp = getattr(ctx.room, "local_participant", None)
                if lp:
                    _root_span.set_attribute("livekit.participant.identity", getattr(lp, "identity", None))
                    _root_span.set_attribute("livekit.participant.sid", getattr(lp, "sid", None))
        except Exception:
            pass
    except Exception as e:
        logger.error(f"Failed to connect to LiveKit room: {e}", exc_info=True)
        try:
            otel_context.detach(_root_token)
            _root_span.end()
        except Exception:
            pass
        return

    # Create the Conductor instance
    logger.info("Creating RoxAgent instance...")
    rox_agent_instance = RoxAgent()
    logger.info("RoxAgent instance created successfully")
    ctx.rox_agent = rox_agent_instance  # Make agent findable by RPC service
    rox_agent_instance._room = ctx.room
    rox_agent_instance.session_id = ctx.room.name

    # Listen for data-channel events and buffer them (imprinting and live monitoring)
    @ctx.room.on("data_received")
    def _on_data_received(data_packet: rtc.DataPacket):
        try:
            payload_bytes = data_packet.data
            payload_str = (
                payload_bytes.decode("utf-8")
                if isinstance(payload_bytes, (bytes, bytearray))
                else str(payload_bytes)
            )
            payload = json.loads(payload_str)
            if isinstance(payload, dict):
                # Imprinter recording packets from browser pod (event key)
                if payload.get("source") == "rrweb" and "event" in payload:
                    rox_agent_instance.rrweb_events_buffer.append(payload["event"])  # type: ignore[arg-type]
                    logger.info(
                        f"[rrweb] Buffered imprint event. Buffer size={len(rox_agent_instance.rrweb_events_buffer)}"
                    )
                # Live monitoring packets from browser pod (event_payload key)
                elif (
                    payload.get("source") in ("rrweb", "vscode")
                    and "event_payload" in payload
                ):
                    rox_agent_instance.live_events_buffer.append(payload)
                    if len(rox_agent_instance.live_events_buffer) % 50 == 0:
                        logger.info(
                            f"[live] Buffered {len(rox_agent_instance.live_events_buffer)} live events so far"
                        )
                else:
                    # Other data packets may be added in the future; ignore here
                    logger.debug(
                        f"[data_received] Non-handled packet: keys={list(payload.keys())}"
                    )
        except Exception as e:
            logger.warning(f"[data_received] Failed to process packet: {e}")

    # --- Participant disconnect handling: delayed shutdown with grace window ---
    student_disconnect_grace = float(os.getenv("STUDENT_DISCONNECT_GRACE_SEC", "12"))
    student_disconnect_task: asyncio.Task | None = None

    async def _delayed_student_shutdown(identity: str):
        try:
            await asyncio.sleep(student_disconnect_grace)
            # If still the recorded caller and no reconnect cancelled us, shutdown
            if identity and identity == rox_agent_instance.caller_identity:
                logger.warning(
                    f"Student '{identity}' did not reconnect within {student_disconnect_grace:.1f}s. Shutting down agent."
                )
                ctx.shutdown()
        except asyncio.CancelledError:
            logger.info(
                "Student reconnect detected before grace timeout; not shutting down."
            )
        except Exception:
            logger.debug("error in delayed student shutdown task", exc_info=True)

    def _on_participant_disconnected(participant: rtc.RemoteParticipant):
        nonlocal student_disconnect_task
        try:
            logger.info(f"Participant disconnected: {participant.identity}")
            # Only act if the student (caller_identity) leaves
            if participant.identity == rox_agent_instance.caller_identity:
                # Cancel any existing shutdown task first
                if student_disconnect_task and not student_disconnect_task.done():
                    student_disconnect_task.cancel()
                # Schedule delayed shutdown
                student_disconnect_task = asyncio.create_task(
                    _delayed_student_shutdown(participant.identity)
                )
        except Exception:
            logger.debug("participant_disconnected handler error", exc_info=True)

    # Register the disconnect listener
    ctx.room.on("participant_disconnected", _on_participant_disconnected)

    # --- Startup timeout: shut down if no student joins within timeout ---
    shutdown_timeout_seconds = int(os.getenv("ROX_NO_SHOW_TIMEOUT_SECONDS", "60"))

    async def shutdown_if_no_student():
        logger.info(
            f"Agent will shut down in {shutdown_timeout_seconds}s if no student joins."
        )
        try:
            await asyncio.sleep(shutdown_timeout_seconds)
        except asyncio.CancelledError:
            logger.info("No-show shutdown task cancelled (student joined).")
            return

        # If this task wasn't cancelled and no caller identity is set, no student joined
        if not rox_agent_instance.caller_identity:
            logger.warning(
                "No student joined within the timeout period. Shutting down agent."
            )
            ctx.shutdown()

    shutdown_task = asyncio.create_task(shutdown_if_no_student())

    # Cancel the shutdown timer if a participant connects (sync callback is enough)
    def _cancel_no_show_on_connect(participant: rtc.RemoteParticipant):
        try:
            logger.info(
                f"Participant connected: {participant.identity}. Cancelling no-show shutdown task."
            )
            if not shutdown_task.done():
                shutdown_task.cancel()
            # Also cancel any pending student disconnect shutdown if the student reconnected
            if "student_disconnect_task" in locals():
                nonlocal student_disconnect_task
                if (
                    participant.identity == rox_agent_instance.caller_identity
                    and student_disconnect_task
                    and not student_disconnect_task.done()
                ):
                    student_disconnect_task.cancel()
        except Exception:
            logger.debug("no-show cancel handler error", exc_info=True)

    ctx.room.on("participant_connected", _cancel_no_show_on_connect)

    # Queue session start when participant connects to avoid race if proactive check misses join
    def _on_participant_connected_start(participant: rtc.RemoteParticipant):
        try:
            pid = getattr(participant, "identity", None)

            async def _do_queue():
                try:
                    # Set caller identity if not already set, prefer non-browser participants
                    if (
                        not rox_agent_instance.caller_identity
                        and pid
                        and not str(pid).startswith("browser-bot-")
                    ):
                        rox_agent_instance.caller_identity = pid
                        logger.info(
                            f"[AUTO] caller_identity set on connect to {pid} (non-browser)"
                        )

                    # Always enqueue a start task on connect to nudge the loop after refresh.
                    # Mark reconnect when session already started to allow idempotent handling downstream.
                    initial_task = {
                        "task_name": "start_tutoring_session",
                        "caller_identity": rox_agent_instance.caller_identity or pid,
                    }
                    if rox_agent_instance._session_started:
                        initial_task["reconnect"] = True
                        logger.info("[AUTO] Queued reconnect start task on participant_connected")
                    else:
                        logger.info("[AUTO] Queued session start task on participant_connected")

                    await rox_agent_instance._processing_queue.put(initial_task)

                    # Only set the started flag the first time
                    if not rox_agent_instance._session_started:
                        rox_agent_instance._session_started = True
                except Exception:
                    logger.debug(
                        "[AUTO] failed queuing start task on participant_connected",
                        exc_info=True,
                    )

            asyncio.create_task(_do_queue())
        except Exception:
            logger.debug(
                "[AUTO] participant_connected start handler error", exc_info=True
            )

    ctx.room.on("participant_connected", _on_participant_connected_start)

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
        rox_agent_instance.curriculum_id = metadata.get(
            "curriculum_id"
        )  # Using 'curriculum_id' variable name

        # Validate that we have the necessary IDs
        if not rox_agent_instance.user_id or not rox_agent_instance.curriculum_id:
            logger.error(
                f"CRITICAL: Missing user_id or curriculum_id in metadata. Agent cannot function."
            )
            try:
                otel_context.detach(_root_token)
                _root_span.end()
            except Exception:
                pass
            return

        rox_agent_instance.current_lo_id = metadata.get("current_lo_id")

        logger.info(
            f"Agent state populated: user_id={rox_agent_instance.user_id}, curriculum_id={rox_agent_instance.curriculum_id}"
        )
    except Exception as e:
        logger.warning(f"Could not parse token metadata, using defaults: {e}")

    # Create AgentSession with audio capabilities and LLM interceptor
    # The LLM interceptor will process user speech and forward to LangGraph
    # Build AgentSession kwargs to allow conditional inclusion of turn detection
    session_kwargs = {
        "stt": deepgram.STT(
            model="nova-2",
            language="en",
            api_key=os.environ.get("DEEPGRAM_API_KEY"),
            interim_results=True,
            punctuate=True,
            smart_format=True,
        ),
        "llm": rox_agent_instance.llm_interceptor,
        "tts": deepgram.TTS(
            model="aura-2-helena-en",
            api_key=os.environ.get("DEEPGRAM_API_KEY"),
            encoding="linear16",
            sample_rate=24000,
        ),
        "vad": silero.VAD.load(
            min_silence_duration=1.0,
            prefix_padding_duration=0.2,
        ),
    }

    # Disable heavy turn detection by default to avoid OOM in inference subprocess.
    # Enable by setting ENABLE_TURN_DETECTION=true. If enabled, prefer the lighter English model.
    if os.getenv("ENABLE_TURN_DETECTION", "false").lower() == "true":
        try:
            model_choice = os.getenv("TURN_DETECTION_MODEL", "english").lower()
            if model_choice == "multilingual":
                from livekit.plugins.turn_detector.multilingual import MultilingualModel as _TDModel
            else:
                from livekit.plugins.turn_detector.english import EnglishModel as _TDModel

            session_kwargs["turn_detection"] = _TDModel(
                unlikely_threshold=float(os.getenv("TURN_UNLIKELY_THRESHOLD", "0.1"))
            )
            logger.info(f"Turn detection enabled using model: {model_choice}")
        except Exception as e:
            logger.warning(
                f"Failed to enable turn detection, proceeding without it: {e}",
                exc_info=True,
            )
    else:
        logger.info("Turn detection disabled (ENABLE_TURN_DETECTION=false). Using VAD-only.")

    main_agent_session = agents.AgentSession(**session_kwargs)
    rox_agent_instance.agent_session = main_agent_session
    # Store the LiveKit room reference for downstream clients (browser/frontend)
    try:
        rox_agent_instance._room = ctx.room
        # Expose session_id for identity composition if needed
        rox_agent_instance.session_id = getattr(ctx.room, "name", None)
    except Exception:
        logger.debug("Unable to cache room/session on agent instance", exc_info=True)

    # Trigger the initial browser navigate only when room has 3 participants (agent + user + browser)
    try:
        browser_identity = None
        if getattr(ctx, "room", None) and getattr(ctx.room, "name", None):
            browser_identity = f"browser-bot-{ctx.room.name}"

        # Helper: send initial navigate only when remote participants >= 2 and browser is present
        async def _maybe_send_initial_nav_if_three():
            try:
                if rox_agent_instance._initial_nav_sent:
                    return
                rp = getattr(ctx.room, "remote_participants", {})
                if not isinstance(rp, dict):
                    return
                # Need at least user + browser (two remotes)
                if browser_identity and browser_identity in rp and len(rp) >= 2:
                    await rox_agent_instance._browser_pod_client.send_browser_command(
                        ctx.room,
                        browser_identity,
                        "browser_navigate",
                        {""},
                    )
                    rox_agent_instance._initial_nav_sent = True
                    logger.info(
                        f"[AUTO] Sent initial navigate to example.com (3 participants detected)"
                    )
            except Exception as e:
                logger.error(
                    f"[AUTO] Failed to send initial navigate (3 participants rule): {e}"
                )

        if browser_identity and not rox_agent_instance._browser_join_cb_registered:

            def _browser_join_cb(p):
                try:
                    pid = getattr(p, "identity", "")
                    # On any participant connect, re-check the 3-participant rule
                    if not rox_agent_instance._initial_nav_sent:
                        asyncio.create_task(_maybe_send_initial_nav_if_three())
                except Exception:
                    logger.debug(
                        "[AUTO][event] participant_connected handler error",
                        exc_info=True,
                    )

            ctx.room.on("participant_connected", _browser_join_cb)
            rox_agent_instance._browser_join_cb_registered = True

            # If browser already present by the time we register, send immediately once
            try:
                rp = getattr(ctx.room, "remote_participants", {})
                if isinstance(rp, dict) and not rox_agent_instance._initial_nav_sent:
                    asyncio.create_task(_maybe_send_initial_nav_if_three())
            except Exception:
                logger.debug("[AUTO] immediate send check failed", exc_info=True)
    except Exception:
        logger.debug("[AUTO] failed to register browser join hook", exc_info=True)

    # Enhanced user state handler for VAD-based automatic mic turn-off
    def _handle_user_state_change(ev):
        logger.info(f"[TURN] User state changed: {ev.old_state} -> {ev.new_state}")

        HANGOVER_MS = int(os.getenv("TURN_HANGOVER_MS", "900"))

        # Cancel any pending turn-end if speech resumes
        if ev.old_state == "listening" and ev.new_state == "speaking":
            try:
                if (
                    rox_agent_instance._pending_turn_end_task
                    and not rox_agent_instance._pending_turn_end_task.done()
                ):
                    rox_agent_instance._pending_turn_end_task.cancel()
                    rox_agent_instance._pending_turn_end_task = None
                    logger.info("[TURN] Cancelled pending turn-end due to resumed speech")
            except Exception:
                logger.debug("[TURN] Failed cancelling pending turn-end", exc_info=True)
            return

        # Schedule a delayed mic-off when user stops speaking
        if ev.old_state == "speaking" and ev.new_state == "listening":
            logger.info("[TURN] Scheduling mic-off after hangover")

            # Cancel any prior pending task
            try:
                if (
                    rox_agent_instance._pending_turn_end_task
                    and not rox_agent_instance._pending_turn_end_task.done()
                ):
                    rox_agent_instance._pending_turn_end_task.cancel()
            except Exception:
                logger.debug("[TURN] Error cancelling old pending task", exc_info=True)

            async def _delayed_end():
                try:
                    await asyncio.sleep(HANGOVER_MS / 1000.0)
                    # Perform mic off and process transcript
                    await rox_agent_instance._handle_vad_mic_off()

                    transcript = ""
                    if (
                        hasattr(rox_agent_instance.llm_interceptor, "_buffer")
                        and rox_agent_instance.llm_interceptor._buffer
                    ):
                        transcript = " ".join(rox_agent_instance.llm_interceptor._buffer)
                        rox_agent_instance.llm_interceptor._buffer.clear()
                        logger.info(f"[TURN] Using buffered transcript: {transcript}")

                    if transcript.strip():
                        await rox_agent_instance._process_user_speech_auto(transcript)
                    else:
                        logger.info("[TURN] No transcript to process after hangover")
                except asyncio.CancelledError:
                    logger.info("[TURN] Hangover task cancelled")
                except Exception:
                    logger.debug("[TURN] Hangover task error", exc_info=True)
                finally:
                    rox_agent_instance._pending_turn_end_task = None

            rox_agent_instance._pending_turn_end_task = asyncio.create_task(_delayed_end())

    # Register the VAD handler AFTER rox_agent_instance is created
    main_agent_session.on("user_state_changed", _handle_user_state_change)

    # --- Register RPC Handlers (The Conductor's "Ears") ---
    agent_rpc_service = AgentInteractionService(ctx=ctx)
    service_name = "rox.interaction.AgentInteraction"
    local_participant = ctx.room.local_participant

    logger.info("Registering specialized RPC handlers...")
    try:
        # Register the new specialized handlers
        local_participant.register_rpc_method(
            f"{service_name}/InvokeAgentTask", agent_rpc_service.InvokeAgentTask
        )
        local_participant.register_rpc_method(
            f"{service_name}/student_wants_to_interrupt",
            agent_rpc_service.student_wants_to_interrupt,
        )
        local_participant.register_rpc_method(
            f"{service_name}/student_mic_button_interrupt",
            agent_rpc_service.student_mic_button_interrupt,
        )
        local_participant.register_rpc_method(
            f"{service_name}/student_spoke_or_acted",
            agent_rpc_service.student_spoke_or_acted,
        )
        local_participant.register_rpc_method(
            f"{service_name}/student_stopped_listening",
            agent_rpc_service.student_stopped_listening,
        )
        local_participant.register_rpc_method(
            f"{service_name}/TestPing", agent_rpc_service.TestPing
        )

        logger.info("All RPC handlers registered successfully")

    except Exception as e:
        logger.error(f"Failed to register RPC handlers: {e}", exc_info=True)
        try:
            otel_context.detach(_root_token)
            _root_span.end()
        except Exception:
            pass
        return  # Cannot continue without RPC handlers

    # --- Send Agent Ready Handshake ---
    try:
        # Wait briefly for participants to join
        await asyncio.sleep(1)

        if len(ctx.room.remote_participants) > 0:
            all_identities = list(ctx.room.remote_participants.keys())
            # Prefer non-browser participants for caller identity
            non_browser = [i for i in all_identities if not str(i).startswith("browser-bot-")]
            selected_identity = (non_browser[0] if non_browser else all_identities[0])
            rox_agent_instance.caller_identity = selected_identity

            logger.info(
                f"Participant selected: {selected_identity}. Sending 'agent_ready' handshake."
            )

            handshake_payload = json.dumps(
                {
                    "type": "agent_ready",
                    "agent_identity": ctx.room.local_participant.identity,
                }
            )

            await ctx.room.local_participant.publish_data(
                payload=handshake_payload,
                destination_identities=[selected_identity],
            )
            logger.info(f"Sent 'agent_ready' to {selected_identity}")
        else:
            logger.warning(
                "No participants in room after 1s, skipping initial handshake"
            )

    except Exception as e:
        logger.error(f"Failed to send 'agent_ready' handshake: {e}", exc_info=True)
    logger.info(f"Number of remote participants: {len(ctx.room.remote_participants)}")
    logger.info(f"Participant identities: {list(ctx.room.remote_participants.keys())}")

    # --- Handshake watcher: keep sending 'agent_ready' to newly joined students ---
    handshaken_identities: set[str] = set()
    try:
        # Record the one we just handshook (if any)
        if 'selected_identity' in locals():
            handshaken_identities.add(selected_identity)
    except Exception:
        pass

    async def _handshake_watcher():
        try:
            while True:
                try:
                    # Snapshot of remote participants
                    rp = getattr(ctx.room, "remote_participants", {})
                    if isinstance(rp, dict):
                        for pid in list(rp.keys()):
                            if str(pid).startswith("browser-bot-"):
                                continue
                            if pid not in handshaken_identities:
                                try:
                                    payload = json.dumps({
                                        "type": "agent_ready",
                                        "agent_identity": ctx.room.local_participant.identity,
                                    })
                                    await ctx.room.local_participant.publish_data(
                                        payload=payload,
                                        destination_identities=[pid],
                                    )
                                    handshaken_identities.add(pid)
                                    # Update caller_identity to the most recent student
                                    rox_agent_instance.caller_identity = pid
                                    logger.info(f"Sent 'agent_ready' (watcher) to {pid}")
                                except Exception:
                                    logger.debug("Failed to send 'agent_ready' in watcher (non-fatal)", exc_info=True)
                    await asyncio.sleep(1.0)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.debug("Handshake watcher loop error (non-fatal)", exc_info=True)
                    await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            logger.info("Handshake watcher cancelled")

    handshake_task = asyncio.create_task(_handshake_watcher())
    logger.info(
        "Conductor fully operational. Starting processing loop and agent session..."
    )
    logger.info("Starting processing loop...")
    # Start the processing loop as a background task
    processing_task = asyncio.create_task(rox_agent_instance.processing_loop())
    logger.info("Processing loop task created")

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
                rox_agent_instance.caller_identity = list(
                    ctx.room.remote_participants.keys()
                )[0]

            logger.info(
                f"Student '{rox_agent_instance.caller_identity}' is present. Queueing proactive start task."
            )

            # Create a special, predefined task.
            # CRITICAL: It has NO 'transcript' because the student hasn't spoken.
            # Only start session if not already started (prevent duplicates)
            if not rox_agent_instance._session_started:
                initial_task = {
                    "task_name": "start_tutoring_session",
                    "caller_identity": rox_agent_instance.caller_identity,
                }

                # Queue this initial task. The processing_loop will pick it up.
                await rox_agent_instance._processing_queue.put(initial_task)
                rox_agent_instance._session_started = True
                logger.info("Student joined: Queued initial session start task")

        else:
            logger.warning(
                "No student in the room. Agent will wait for a participant to join."
            )

    except Exception as e:
        logger.error(f"Failed during proactive start sequence: {e}", exc_info=True)

    # Keep the agent alive by waiting for the processing task
    await processing_task

    # Close the root span and detach context once the job ends
    try:
        try:
            handshake_task.cancel()
            with contextlib.suppress(Exception):
                await handshake_task
        except Exception:
            pass
        otel_context.detach(_root_token)
        _root_span.end()
    except Exception:
        pass


# FastAPI application instance for HTTP service mode
app = FastAPI(
    title="Rox Agent Service", description="Unified LiveKit Agent and HTTP API"
)

# Instrument the FastAPI app with OpenTelemetry
try:
    FastAPIInstrumentor.instrument_app(app)
    logger.info("FastAPI app has been instrumented for OpenTelemetry.")
except Exception as e:
    logger.error(f"Failed to instrument FastAPI app: {e}")

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
            detail="Server configuration error: LIVEKIT_API_KEY and LIVEKIT_API_SECRET must be set.",
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
            "connect",  # The command for the CLI
            "--url",  # The URL flag
            room_url,  # The URL value
            "--room",  # The room flag
            room_name,
            "--api-key",  # The API key flag
            api_key,
            "--api-secret",  # The API secret flag
            api_secret,
        ]
        print(f"Executing command: {' '.join(command)}")

        # Run the agent as a non-blocking subprocess
        process = subprocess.Popen(command)

        return {"message": f"Agent started for room {room_name}", "pid": process.pid}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to run agent: {str(e)}")



class BrowserCommandRequest(BaseModel):
    room_name: str
    tool_name: str
    parameters: Optional[Dict[str, Any]] = None
    room_url: Optional[str] = None
    token: Optional[str] = None
    participant_identity: Optional[str] = None


@app.post("/dev/send-browser-command")
async def dev_send_browser_command(req: BrowserCommandRequest):
    """Dev-only: send a single browser command to the session-bubble via LiveKit.

    Body: {
      room_name: string,
      tool_name: string,            # e.g., 'browser_navigate', 'browser_click'
      parameters?: object,          # e.g., { url: 'https://example.com' }
      room_url?: string,            # LiveKit WS URL (optional)
      token?: string,               # LiveKit access token (optional)
      participant_identity?: string # publish identity (optional)
    }
    """
    try:
        room_name = req.room_name
        tool_name = req.tool_name
        params = req.parameters or {}

        # Resolve LiveKit URL and token
        ws_url = req.room_url or os.getenv("LIVEKIT_URL")
        token = req.token

        # Use a dedicated agent identity to avoid kicking the viewer (student identity)
        agent_identity = req.participant_identity or f"cmd-relay-{room_name}"

        if not token:
            api_key = os.getenv("LIVEKIT_API_KEY")
            api_secret = os.getenv("LIVEKIT_API_SECRET")
            if not (api_key and api_secret):
                raise HTTPException(
                    status_code=500,
                    detail="LIVEKIT_API_KEY/SECRET not set; cannot mint agent token. Pass 'token' explicitly to avoid using the student's token.",
                )
            try:
                # Mint a short-lived token for this dedicated identity
                from livekit.api import AccessToken, VideoGrants

                grants = VideoGrants(
                    room_join=True, room=room_name, can_publish_data=True
                )
                token = (
                    AccessToken(api_key, api_secret)
                    .with_identity(agent_identity)
                    .with_name(agent_identity)
                    .with_grants(grants)
                    .to_jwt()
                )
            except Exception as e:
                logger.error(f"Failed to mint agent token: {e}")
                raise HTTPException(
                    status_code=500, detail="Failed to mint agent token"
                )

        if not ws_url:
            raise HTTPException(
                status_code=500,
                detail="Could not resolve LiveKit wsUrl. Set LIVEKIT_URL or pass room_url.",
            )

        # Connect, publish command, disconnect
        room = rtc.Room()
        await room.connect(ws_url, token)
        try:
            browser_identity = f"browser-bot-{room_name}"
            client = BrowserPodClient()
            ok = await client.send_browser_command(
                room, browser_identity, tool_name, params
            )
            await asyncio.sleep(0.4)
            return {
                "ok": bool(ok),
                "room": room.name,
                "tool": tool_name,
                "parameters": params,
            }
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


# --- Compatibility: expose /local-spawn-agent on this server ---
class AgentSpawnRequest(BaseModel):
    room_name: str
    ws_url: str
    api_key: str
    api_secret: str
    agent_identity: str
    student_token_metadata: str


async def _run_worker_from_payload(payload: AgentSpawnRequest):
    """Spawn a LiveKit agent worker using the provided payload."""
    try:
        # Mirror local_test_server.py behavior
        os.environ["LIVEKIT_URL"] = payload.ws_url
        os.environ["LIVEKIT_API_KEY"] = payload.api_key
        os.environ["LIVEKIT_API_SECRET"] = payload.api_secret
        os.environ["LIVEKIT_ROOM_NAME"] = payload.room_name
        os.environ["STUDENT_TOKEN_METADATA"] = payload.student_token_metadata

        # Create and run the worker
        worker_options = agents.WorkerOptions(
            entrypoint_fnc=entrypoint,
            ws_url=payload.ws_url,
            api_key=payload.api_key,
            api_secret=payload.api_secret,
        )
        worker = agents.Worker(worker_options)
        await worker.run()
    except Exception as e:
        logger.error(f"[local-spawn-agent] Worker failed: {e}", exc_info=True)


@app.post("/local-spawn-agent")
async def local_spawn_agent(request: AgentSpawnRequest):
    """Dev worker endpoint used by webrtc-token-service in development.

    Accepts the same payload as livekit-service/local_test_server.py and starts the
    LiveKit agent worker in the background.
    """
    try:
        # Schedule the worker to run asynchronously and return immediately
        asyncio.create_task(_run_worker_from_payload(request))
        return {
            "status": "accepted",
            "message": f"Agent spawn scheduled for room {request.room_name}",
        }
    except Exception as e:
        logger.error(f"/local-spawn-agent failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


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
