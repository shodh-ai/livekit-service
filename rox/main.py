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
import time  # Added for silence-gap measurement
import uuid
import base64
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from livekit.plugins import noise_cancellation
from livekit.plugins import deepgram, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel
from custom_llm import CustomLLMBridge
# Add project root to path for clean imports
project_root = Path(__file__).resolve().parent
sys.path.insert(0, str(project_root))

from dotenv import load_dotenv
from typing import Optional, Dict, Any

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
    # Action constant for reactive listening
    ACTION_LISTEN_STREAM = "LISTEN_AND_STREAM_TO_LLM"
    def __init__(self, **kwargs):
        # Set default instructions if not provided, to satisfy Agent base class
        kwargs.setdefault(
            "instructions",
            "You are Rox, an AI assistant for students using the learning platform. You help students understand their learning status and guide them through their learning journey."
        )
        super().__init__(**kwargs)
        self.task_queue: asyncio.Queue[tuple] = asyncio.Queue()
        self.agent_session: Optional[agents.AgentSession] = None
        self.transcript_queue: asyncio.Queue[str] = asyncio.Queue() # Add this back!
        self._room: Optional[rtc.Room] = None
        self._job_ctx: Optional[JobContext] = None

    # Add this back from Opinion 2's code
    def on_transcript(self, transcript: str, participant: rtc.RemoteParticipant, is_final: bool):
        """
        Callback fired by AgentSession whenever STT produces a transcript.
        AgentSession invokes this *synchronously*, so the handler itself must be
        synchronous; otherwise the coroutine would never be awaited and the
        body would not run. We therefore enqueue the transcript with
        `put_nowait`.
        """
        # Ignore empty strings that some STT engines emit.
        if transcript.strip():
            logger.info(
                "Transcript received from %s: '%s' (is_final=%s)",
                participant.identity,
                transcript,
                is_final,
            )
            # Non-blocking enqueue; Queue is unbounded by default.
            self.transcript_queue.put_nowait(transcript)

    async def handle_listen_and_stream(self, caller_identity: str):
        """Reactive listen that commits the user turn after a silence gap."""
        logger.info("Reactive listen initiated – STT->LLM streaming active.")

        # Notify UI that we are listening
        await trigger_client_ui_action(
            self._room,
            caller_identity,
            "START_LISTENING_VISUAL",
            {},
        )

        # Parameters
        silence_gap_s = float(os.getenv("REACTIVE_LISTEN_SILENCE_GAP_S", "1.0"))
        poll_interval_s = 0.1
        last_speaking_ts: Optional[float] = None

        logger.debug(
            "Reactive listen loop starting (silence_gap=%ss poll=%ss)",
            silence_gap_s,
            poll_interval_s,
        )

        # Wait until the user has stopped speaking for `silence_gap_s` seconds
        while True:
            await asyncio.sleep(poll_interval_s)
            user_state = self.agent_session.user_state if self.agent_session else None
            if user_state == "speaking":
                last_speaking_ts = time.time()
                logger.debug("User speaking… resetting silence timer (ts=%s)", last_speaking_ts)
            elif last_speaking_ts is None:
                # Haven't heard the user yet; keep waiting
                continue
            elif (time.time() - last_speaking_ts) >= silence_gap_s:
                logger.debug("Silence gap exceeded (%.2fs ≥ %.2fs). Committing user turn.",
                             time.time() - last_speaking_ts,
                             silence_gap_s)
                break

        try:
            self.agent_session.commit_user_turn()
            logger.info("User turn committed to AgentSession – STT transcript handed to LLM.")
        except Exception as e:
            logger.error("Failed to commit user turn: %s", e, exc_info=True)

        # Notify UI that listening has ended
        await trigger_client_ui_action(
            self._room,
            caller_identity,
            "STOP_LISTENING_VISUAL",
            {},
        )

    async def speak_text(self, text: str):
        # A simple helper for one-off speech
        if self.agent_session:
            await self.agent_session.say(text, allow_interruptions=False) # Don't allow interruptions for small prompts


    async def wait_for_user_transcript(self, listen_config: dict, caller_identity: str) -> Optional[str]:
        """
        An "Active Listener" that cleans up stale data and uses a more patient timeout.
        """
        # FIX 1: Increase the silence timeout to be more forgiving of network latency.
        # 2.0 seconds was too short. 3.0 is a much safer default.
        silence_timeout = listen_config.get("silence_timeout_s", 5.0) 
    
        logger.info(f"Active Listener starting. Silence timeout: {silence_timeout}s")

    # FIX 2: Perform queue hygiene. Empty any stale transcripts from a previous,
    # timed-out listen attempt before we start waiting for the new one.
        while not self.transcript_queue.empty():
            stale_transcript = self.transcript_queue.get_nowait()
            logger.warning(f"Discarding stale transcript from queue: '{stale_transcript}'")

        # Send the UI action to show we're listening
        await trigger_client_ui_action(
            self._room,
            caller_identity,
            "START_LISTENING_VISUAL",
            {}
        )

        full_transcript = ""
        while True:
            try:
                new_chunk = await asyncio.wait_for(
                    self.transcript_queue.get(),
                    timeout=silence_timeout
                )
                full_transcript += f" {new_chunk}"
            
            # (Optional but good) De-dupe the queue if multiple chunks arrive at once
                while not self.transcript_queue.empty():
                    full_transcript += f" {self.transcript_queue.get_nowait()}"

            except asyncio.TimeoutError:
                logger.info("Active listener timed out. User has finished speaking.")
                break

        full_transcript = full_transcript.strip()

        # Tell the UI we're done listening
        await trigger_client_ui_action(
            self._room,
            caller_identity,
        "STOP_LISTENING_VISUAL",
        {}
    )

        if full_transcript:
            logger.info(f"Final collected transcript: '{full_transcript}'")
            return full_transcript
        else:
            logger.info("No speech was detected during the listening window.")
            prompt = listen_config.get("prompt_if_silent")
            if prompt:
                await self.speak_text(prompt)
            return None
    async def handle_speak_then_listen(self, parameters: dict, caller_identity: str):
        logger.info(f"Handling SPEAK_THEN_LISTEN for {caller_identity}")
        text_to_speak = parameters.get("text_to_speak")
        listen_config = parameters.get("listen_config", {})

        try:
            if text_to_speak:
                logger.info("Speaking with interruptions enabled.")
                await self.agent_session.say(text_to_speak, allow_interruptions=True)

            logger.info("Speech completed naturally. Now listening.")
            transcript = await self.wait_for_user_transcript(listen_config, caller_identity)

            if transcript:
                logger.info(f"User responded: {transcript}. Triggering 'handle_student_response'.")
                await self.trigger_langgraph_task(
                    task_name="handle_student_response",
                    json_payload=json.dumps({"transcript": transcript}),
                    caller_identity=caller_identity
                )
            else:
                logger.info("User was silent after speech. Triggering 'handle_student_silence'.")
                await self.trigger_langgraph_task(
                    task_name="handle_student_silence",
                    json_payload=json.dumps({"speech_id": parameters.get("speech_id")}),
                    caller_identity=caller_identity
                )

        # --- THIS IS THE CORRECTED EXCEPTION NAME ---
        except agents.tts.TTSCancelled:
            logger.warning("HANDLED INTERRUPT: Speech was cancelled by user.")
            
            await self.speak_text("Of course, what's on your mind?")
            user_doubt = await self.wait_for_user_transcript({}, caller_identity)

            if not user_doubt:
                logger.info("User interrupted but said nothing. Ending this flow.")
                return

            context = {
                "text_that_was_being_spoken": text_to_speak,
                "user_doubt": user_doubt
            }

            logger.info("Triggering 'resolve_student_doubt' for LangGraph.")
            await self.trigger_langgraph_task(
                task_name="resolve_student_doubt",
                json_payload=json.dumps(context),
                caller_identity=caller_identity
            )

    async def trigger_langgraph_task(self, task_name: str, json_payload: str, caller_identity: str):
        """Puts a new task onto the agent's processing queue."""
        logger.info(f"Queueing LangGraph task: '{task_name}' for user '{caller_identity}'.")
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
            task_name, json_payload, caller_identity = await self.task_queue.get()
            
            # Here you would fetch the plan from LangGraph
            # For this example, let's assume `plan` is the parsed JSON response
            
            # A mock `plan` for demonstration
            # In reality, you'd make the aiohttp call to LangGraph here
            langgraph_plan = await self.fetch_plan_from_langgraph(task_name, json_payload)

            if not langgraph_plan or not langgraph_plan.get("final_ui_actions"):
                continue

            # Process all actions in the plan
            for action in langgraph_plan["final_ui_actions"]:
                action_type = action.get("action_type")
                parameters = action.get("parameters", {})

                if action_type == "SPEAK_THEN_LISTEN":
                # Reactive: speak (if needed) then rely on STT->LLM streaming
                    text_to_speak = parameters.get("text_to_speak") or parameters.get("text")
                    if text_to_speak:
                        logger.info("Reactive SPEAK_THEN_LISTEN: speaking then entering streaming listen mode.")
                        await self.agent_session.say(text_to_speak, allow_interruptions=True)
                    await self.handle_listen_and_stream(caller_identity)
                elif action_type == "LISTEN_AND_STREAM_TO_LLM":
                # Legacy queued behaviour: collect transcript then process
                    await self.handle_speak_then_listen(parameters, caller_identity)
                elif action_type == "SPEAK_TEXT": # For simple, non-interactive speech
                    await self.speak_text(parameters.get("text", ""))
                else: # For all other simple UI actions
                    await trigger_client_ui_action(
                        self._room, caller_identity, action_type, parameters
                    )


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
    llm_bridge = CustomLLMBridge()

    main_agent_session = agents.AgentSession(  # Renamed for clarity
        stt=deepgram.STT(model="nova-2", language="multi"),  # nova-2 or nova-3
        llm=llm_bridge,
        tts=deepgram.TTS(model="aura-asteria-en", api_key=os.environ.get("DEEPGRAM_API_KEY")),
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