#!/usr/bin/env python3
"""
Rox Assistant LiveKit Agent - Corrected and Refactored
"""
from livekit.plugins import google
import os
import sys
import logging
import argparse
import asyncio
import json
import uuid
import base64
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from livekit.plugins import noise_cancellation
from livekit.plugins import deepgram, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel
from livekit.agents.llm import LLM, ChatChunk, ChoiceDelta, ChatContext# Add project root to path for clean imports
project_root = Path(__file__).resolve().parent
sys.path.insert(0, str(project_root))

from dotenv import load_dotenv
from typing import Optional, Dict, Any
from contextlib import asynccontextmanager

# Third-party imports
import aiohttp
from livekit import rtc, agents
from livekit.agents import Agent, JobContext, RoomInputOptions, WorkerOptions

# Local application imports
from generated.protos import interaction_pb2
from rpc_services import AgentInteractionService
from utils.ui_action_factory import build_ui_action_request # +++ Your factory is crucial

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)




# --- Environment Loading and Validation ---
load_dotenv(dotenv_path=project_root / ".env")
# ... (Your environment variable loading and validation is fine, keep it as is) ...
# For brevity, I'll assume your env loading block is here.

# Define the RPC method name that the agent calls on the client
CLIENT_RPC_FUNC_PERFORM_UI_ACTION = "rox.interaction.ClientSideUI/PerformUIAction"

# +++ REFACTORED AND SIMPLIFIED +++
async def trigger_client_ui_action(
    room: rtc.Room,
    client_identity: str,
    action_type: str,
    parameters: Optional[Dict[str, Any]] = None,
) -> Optional[interaction_pb2.ClientUIActionResponse]:
    """
    Builds and sends a UI action RPC to a client using the action factory.
    This function is now simple and never needs to be modified.
    """
    if not client_identity:
        logger.error("B2F RPC: Client identity was not provided. Cannot send UI action.")
        return None

    try:
        logger.info(f"Building UI action '{action_type}' for client '{client_identity}'.")
        # 1. Build the Protobuf message using the factory
        request_pb = build_ui_action_request(action_type, parameters or {})
        request_pb.request_id = f"ui-{uuid.uuid4().hex[:8]}"

        # 2. Serialize and send the RPC
        rpc_method_name = f"rox.interaction.ClientSideUI/PerformUIAction"
        payload_bytes = request_pb.SerializeToString()
        base64_encoded_payload = base64.b64encode(payload_bytes).decode("utf-8")

        logger.info(f"Sending RPC '{rpc_method_name}' to '{client_identity}'. Action: {action_type}")

        response_payload_str = await room.local_participant.perform_rpc(
            destination_identity=client_identity,
            method=rpc_method_name,
            payload=base64_encoded_payload,
        )
        response_bytes = base64.b64decode(response_payload_str)
        response_pb = interaction_pb2.ClientUIActionResponse()
        response_pb.ParseFromString(response_bytes)

        logger.info(f"B2F RPC Response from '{client_identity}': Success={response_pb.success}")
        return response_pb

    except Exception as e:
        logger.error(f"Failed to send UI action RPC to '{client_identity}': {e}", exc_info=True)
        return None


class RoxAgent(Agent):
    class TranscriptInterceptor(LLM):
        """LLM shim that intercepts user speech, enqueues a LangGraph task, and yields no TTS."""
        def __init__(self, outer: "RoxAgent", debounce_ms: int = 500):
            super().__init__()
            self._outer = outer
            self._debounce_ms = debounce_ms
            self._buffer: list[str] = []
            self._debounce_handle: Optional[asyncio.TimerHandle] = None

        def chat(self, *, chat_ctx: ChatContext = None, tools=None, tool_choice=None):  # noqa: D401
            return self._chat_ctx_mgr(chat_ctx)

        @asynccontextmanager
        async def _chat_ctx_mgr(self, chat_ctx: ChatContext):  # noqa: D401
            # Extract latest user transcript
            transcript = ""
            if chat_ctx:
                messages = getattr(chat_ctx, "_items", [])
                for msg in reversed(messages):
                    role = getattr(msg, "role", None) or (msg.get("role") if isinstance(msg, dict) else None)
                    if str(role).lower() == "user":
                        transcript = getattr(msg, "content", None) or (
                            msg.get("content") if isinstance(msg, dict) else None
                        )
                        if isinstance(transcript, list):
                            transcript = " ".join(map(str, transcript))
                        transcript = str(transcript)
                        break
            if transcript:
                self._buffer.append(transcript)
                # reset debounce timer
                if self._debounce_handle:
                    self._debounce_handle.cancel()
                loop = asyncio.get_event_loop()
                self._debounce_handle = loop.call_later(self._debounce_ms / 1000.0, lambda: asyncio.create_task(self._flush()))
            # Yield a single empty chunk so that AgentSession does not trigger TTS.
            try:
                yield self._stream_empty()
            finally:
                pass

        async def _flush(self):
            full_transcript = " ".join(self._buffer)
            self._buffer.clear()
            logger.info(f"Intercepted transcript: {full_transcript}")

            # Read the context name that was set by handle_speak_then_listen
            task_name = self._outer.next_turn_context_name

            if not task_name or task_name == "GENERAL_CONVERSATION_TURN":
                 logger.warning(f"No specific context set for this turn. Routing to generic handler.")
                 # Fallback to a generic task if needed
                 task_name = "handle_student_response"

            turn_payload = {
                "transcript": full_transcript,
                "current_context": {
                    "user_id": self._outer.user_id,
                    "session_id": self._outer.session_id,
                    "task_stage": task_name # Pass the context as the task_stage
                }
            }
            
            # The task_name now correctly reflects the flow we're in
            await self._outer.trigger_langgraph_task(
                task_name=task_name,
                json_payload=json.dumps(turn_payload),
                caller_identity=self._outer.caller_identity
            )

        async def _stream_empty(self):
            yield ChatChunk(id=str(uuid.uuid4()), delta=ChoiceDelta(role="assistant", content=""))

    def __init__(self, **kwargs):
        # Set default instructions if not provided, to satisfy Agent base class
        kwargs.setdefault(
            "instructions",
            "You are Rox, an AI assistant for students using the learning platform. You help students understand their learning status and guide them through their learning journey."
        )
        super().__init__(**kwargs)
        self.task_queue: asyncio.Queue[tuple] = asyncio.Queue()
        self.agent_session: Optional[agents.AgentSession] = None
        self.user_spoke_event = asyncio.Event()
        self._room: Optional[rtc.Room] = None
        self._job_ctx: Optional[JobContext] = None
        # LLM interceptor instance
        self.llm_interceptor = RoxAgent.TranscriptInterceptor(self)

        # --- ADD THESE LINES ---
        # These attributes will be set by the RPC service.
        self.user_id: Optional[str] = None
        self.session_id: Optional[str] = None
        self.caller_identity: Optional[str] = None
        self.agent_context: Dict[str, Any] = {} # For dynamic routing from frontend
        self.next_turn_context_name: str = "GENERAL_CONVERSATION_TURN"
        # --- END OF ADDED LINES ---

    async def speak_text(self, text: str):
        # A simple helper for one-off speech
        if self.agent_session:
            await self.agent_session.say(text, allow_interruptions=False) # Don't allow interruptions for small prompts

    async def handle_speak_then_listen(self, parameters: dict, caller_identity: str):
        text_to_speak = parameters.get("text_to_speak")
        listen_config = parameters.get("listen_config", {})
        timeout_s = listen_config.get("silence_timeout_s", 7.0)
        prompt_if_silent = listen_config.get("prompt_if_silent")

        # Before listening, store the context that LangGraph provided for the NEXT turn.
        # If it's not provided, fall back to a generic name.
        self.next_turn_context_name = listen_config.get("context_for_next_turn", "GENERAL_CONVERSATION_TURN")
        logger.info(f"Agent is now listening. Next user speech will be handled as: '{self.next_turn_context_name}'")

        try:
            if text_to_speak and self.agent_session:
                await self.agent_session.say(text_to_speak, allow_interruptions=False)

            self.user_spoke_event.clear()
            await trigger_client_ui_action(self._room, caller_identity, "START_LISTENING_VISUAL", {})

            await asyncio.wait_for(self.user_spoke_event.wait(), timeout=timeout_s)
            logger.info("Listen successful: user spoke and task was queued by interceptor.")

        except asyncio.TimeoutError:
            logger.warning("Listen timed out: user did not speak.")
            # If a silent prompt was provided, say it.
            if prompt_if_silent:
                await self.speak_text(prompt_if_silent)

        except agents.llm.LLMStreamInterrupted:
            logger.info("TTS was cancelled by user interrupt.")

        finally:
            await trigger_client_ui_action(self._room, caller_identity, "STOP_LISTENING_VISUAL", {})

    async def trigger_langgraph_task(self, task_name: str, json_payload: str, caller_identity: str):
        """Puts a new task onto the agent's processing queue."""
        logger.info(f"Queueing LangGraph task: '{task_name}' for user '{self.user_id}'.")
        await self.task_queue.put((task_name, json_payload, caller_identity))

    async def fetch_plan_from_langgraph(self, task_name: str, json_payload: str) -> Optional[dict]:
        logger.info(f"Connecting to LangGraph for task '{task_name}'...")
        langgraph_url = os.getenv("MY_CUSTOM_AGENT_URL")
        if not langgraph_url:
            logger.error("MY_CUSTOM_AGENT_URL is not set. Cannot contact LangGraph.")
            return None

        endpoint = urlunparse(urlparse(langgraph_url)._replace(path="/invoke_task_streaming"))
        request_body = {"task_name": task_name, "json_payload": json_payload}
        
        final_response = None
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(endpoint, json=request_body) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error(f"LangGraph service returned error {response.status}: {error_text}")
                        return None

                    # Your SSE parsing logic can be used here.
                    # The goal is to find the event that contains the final plan.
                    # Assuming your graph sends an event named "final_response" at the end.
                    async for event in self.parse_sse_stream(response): 
                        if event.get("event_name") == "final_response":
                            logger.info("Received final_response from LangGraph.")
                            final_response = event.get("data")
                            break # We have the full plan, so we can stop listening for events
            return final_response
        except Exception as e:
            logger.error(f"Failed to fetch plan from LangGraph: {e}", exc_info=True)
            return None

    async def parse_sse_stream(self, response):
        current_event_name = None
        current_event_data_lines = []
        async for line_bytes in response.content:
            line = line_bytes.decode('utf-8').strip()
            if line.startswith('event:'):
                current_event_name = line[len('event:'):].strip()
            elif line.startswith('data:'):
                current_event_data_lines.append(line[len('data:'):].strip())
            elif not line and current_event_name:
                data_str = "\n".join(current_event_data_lines)
                yield {"event_name": current_event_name, "data": json.loads(data_str)}
                current_event_name = None
                current_event_data_lines = []

    async def processing_loop(self):
        logger.info("Agent's main processing loop started.")
        while True:
            try:
                task_name, json_payload, caller_identity = await self.task_queue.get()

                # Fetch execution plan from LangGraph
                langgraph_plan = await self.fetch_plan_from_langgraph(task_name, json_payload)
                if not langgraph_plan:
                    continue

                logger.info(f"Received LangGraph plan: {langgraph_plan}")

                # Shortcut: if LG plan simply contains text_for_tts, speak it directly
                text_to_speak = langgraph_plan.get("text_for_tts")
                if text_to_speak:
                    await self.speak_text(text_to_speak)
                    # still fall through to any ui actions if present

                if not langgraph_plan.get("final_ui_actions"):
                    continue

                # Execute all actions in the plan
                for action in langgraph_plan["final_ui_actions"]:
                    action_type = action.get("action_type")
                    parameters = action.get("parameters", {})

                    try:
                        if action_type == "SPEAK_THEN_LISTEN":
                            await self.handle_speak_then_listen(parameters, caller_identity)
                        elif action_type == "SPEAK_TEXT":
                            await self.speak_text(parameters.get("text", ""))
                        elif action_type in {"SHOW_HTML", "SET_AVATAR_EXPRESSION"}:
                            await trigger_client_ui_action(self._room, caller_identity, action_type, parameters)
                        else:
                            logger.warning("Unknown action_type '%s', forwarding to UI layer.", action_type)
                            await trigger_client_ui_action(self._room, caller_identity, action_type, parameters)
                    except Exception as action_err:
                        logger.error("Error executing action '%s': %s", action_type, action_err, exc_info=True)
            except Exception as loop_err:
                logger.exception("Processing loop recovered from unexpected error: %s", loop_err)
                await asyncio.sleep(1)


async def entrypoint(ctx: JobContext):
    """Main entrypoint for the agent job."""
    logger.info(f"Agent job starting for room '{ctx.room.name}'.")

    try:
        await ctx.connect()
        logger.info(f"Successfully connected to LiveKit room '{ctx.room.name}'")
    except Exception as e:
        logger.error(f"Failed to connect to LiveKit room: {e}", exc_info=True)
        return

    # --- Agent and Session Setup ---
    # This block can remain largely as you had it, setting up the agent instance
    # and the main VAD/STT/TTS session.
    
    rox_agent_instance = RoxAgent()
    ctx.rox_agent = rox_agent_instance  # Make agent findable by the RPC service
    rox_agent_instance._job_ctx = ctx
    rox_agent_instance._room = ctx.room
    
    # Instantiate minimal LLM bridge that forwards transcripts to the FastAPI backend.

    main_agent_session = agents.AgentSession(  # Renamed for clarity
        stt=deepgram.STT(model="nova-2", language="multi"),  # nova-2 or nova-3
        llm=rox_agent_instance.llm_interceptor,
        tts=deepgram.TTS(model="aura-2-helena-en", api_key=os.environ.get("DEEPGRAM_API_KEY")),
        vad=silero.VAD.load(),
        turn_detection=MultilingualModel(),
    )
    rox_agent_instance.agent_session = main_agent_session

    # ------------------------------------------------------------------
    # DEBUG: Log user_state transitions to verify VAD / turn-detection.
    # This helps diagnose "turn detection never fires" (Checklist item #4).
    # ------------------------------------------------------------------
    def _log_user_state(ev):
        logger.debug("AgentSession user_state changed: %s -> %s", ev.old_state, ev.new_state)

    main_agent_session.on("user_state_changed", _log_user_state)
    
    # --- RPC REGISTRATION (THE CRITICAL FIX) ---
    # This is where we register all the methods the frontend can call.
    
    agent_rpc_service = AgentInteractionService(ctx=ctx)
    service_name = "rox.interaction.AgentInteraction"
    local_participant = ctx.room.local_participant
    
    logger.info("Registering all RPC handlers...")
    try:
        local_participant.register_rpc_method(f"{service_name}/InvokeAgentTask", agent_rpc_service.InvokeAgentTask)
        local_participant.register_rpc_method(f"{service_name}/RequestInterrupt", agent_rpc_service.RequestInterrupt)
        local_participant.register_rpc_method(f"{service_name}/TestPing", agent_rpc_service.TestPing)
        
        logging.info("agent rpc handlers registered")

    except Exception as e:
        logger.error(f"Failed to register one or more RPC handlers: {e}", exc_info=True)
        return # Cannot continue without RPC handlers

    # +++ NEW HANDSHAKE LOGIC +++
    # Now that we are ready, tell the client.
    try:
        # Wait for the first participant to join to send the handshake
        await asyncio.sleep(1) # Give a moment for the client to appear
        
        if len(ctx.room.remote_participants) > 0:
            first_participant_identity = list(ctx.room.remote_participants.keys())[0]
            logging.info(f"First participant joined: {first_participant_identity}. Sending 'agent_ready' handshake.")
            
            handshake_payload = json.dumps({
                "type": "agent_ready",
                "agent_identity": ctx.room.local_participant.identity,
            })
            
            await ctx.room.local_participant.publish_data(
                payload=handshake_payload,
                destination_identities=[first_participant_identity],
            )
            logging.info(f"Sent 'agent_ready' to {first_participant_identity}")
        else:
            logging.warning("No participants in the room after 1s, skipping initial handshake.")

    except Exception as e:
        logging.error(f"Failed to send 'agent_ready' handshake: {e}", exc_info=True)

    logger.info("Rox agent fully operational. Starting main session and processing loop...")
    
    # +++ START THE PROCESSING LOOP AS A BACKGROUND TASK +++
    processing_task = asyncio.create_task(rox_agent_instance.processing_loop())

    # Start the main agent session for VAD/STT (voice activity)
    await main_agent_session.start(
        room=ctx.room,
        agent=rox_agent_instance,
    )
    
    # The agent will now stay alive, and the processing_task will handle RPCs
    # in the background.
    await processing_task # This will keep the entrypoint alive

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