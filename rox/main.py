#!/usr/bin/env python3
"""
Rox Assistant LiveKit Agent - Corrected and Refactored
"""

import os
import sys
import logging
import argparse
import asyncio
import json
import uuid
import base64
from pathlib import Path
from typing import Dict, Optional

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
    """Rox AI assistant with robust, concurrent task and interruption handling.""" 
    def __init__(self, **kwargs):
        super().__init__(instructions="You are a helpful voice assistant.", **kwargs)
        self.agent_session: Optional[agents.AgentSession] = None
        self._room: Optional[rtc.Room] = None
        self._job_ctx: Optional[JobContext] = None
        self.task_queue: asyncio.Queue[tuple] = asyncio.Queue()
        self.transcript_queue: asyncio.Queue[str] = asyncio.Queue()
        self.active_tasks: Dict[str, asyncio.Task] = {}
        self.current_tts_task: Optional[asyncio.Task] = None

    async def speak_text(self, text: str):
        """Cancels any previous speech and starts a new TTS task."""
        # Cancel any TTS task that's already running.
        if self.current_tts_task and not self.current_tts_task.done():
            self.current_tts_task.cancel()
            logger.info("Interrupted previous TTS task.")

        # Create and store the new TTS task.
        self.current_tts_task = asyncio.create_task(self._execute_tts(text))
        await self.current_tts_task

    async def _execute_tts(self, text: str):
        """Helper to execute the TTS call, allowing it to be cancelled."""
        if not self.agent_session: return
        try:
            logger.info(f"Speaking: '{text[:50]}...'")
            await self.agent_session.say(text, allow_interruptions=True)
            logger.info(f"Finished speaking: '{text[:50]}...'")
        except asyncio.CancelledError:
            logger.info(f"TTS task for '{text[:50]}...' was explicitly cancelled.")
            # Clear the session's internal TTS queue to stop any pending speech
            self.agent_session.interrupt()
        except Exception as e:
            logger.error(f"Error during TTS playback: {e}", exc_info=True)

    async def on_transcript(self, transcript: str, participant: rtc.RemoteParticipant, is_final: bool):
        if is_final:
            logger.info(f"Transcript received from {participant.identity}: '{transcript}'")
            # Put the transcript on the queue for the handle_listen_step to pick up.
            await self.transcript_queue.put(transcript)

    async def trigger_langgraph_task(self, task_name: str, json_payload: str, caller_identity: str):
        """Handles an incoming RPC, manages interruptions, and queues the new task."""
        task_id = f"task-{uuid.uuid4().hex[:8]}"

        # If this is an interruption, cancel all other active tasks.
        if task_name == "user_wants_to_interrupt":
            logger.warning(f"INTERRUPTION triggered by {caller_identity}. Cancelling all active tasks.")
            active_task_ids = list(self.active_tasks.keys())
            for active_task_id in active_task_ids:
                task_to_cancel = self.active_tasks.get(active_task_id)
                if task_to_cancel:
                    task_to_cancel.cancel()
                    logger.info(f"Cancellation requested for task {active_task_id}.")
            # Also cancel any standalone TTS that might be running
            if self.current_tts_task and not self.current_tts_task.done():
                 self.current_tts_task.cancel()

        logger.info(f"Queueing task '{task_name}' with ID {task_id}.")
        await self.task_queue.put((task_name, json_payload, caller_identity, task_id))

    async def _handle_single_task(self, task_name: str, json_payload: str, caller_identity: str, task_id: str):
        logger.info(f"Background processing started for task {task_id} ('{task_name}').")
        try:
            langgraph_url = os.getenv("MY_CUSTOM_AGENT_URL")
            if not langgraph_url: raise ValueError("MY_CUSTOM_AGENT_URL not set")
            endpoint = urlunparse(urlparse(langgraph_url)._replace(path="/invoke_task_streaming"))
            request_body = {"task_name": task_name, "json_payload": json_payload}
            
            async with aiohttp.ClientSession() as session:
                async with session.post(endpoint, json=request_body) as response:
                    if response.status != 200:
                        raise Exception(f"LangGraph service returned status {response.status}")

                    async for event_name, data_json in self.parse_sse_stream(response):
                        if asyncio.current_task().cancelled(): break

                        # --- THIS IS THE FIX ---
                        # We only care about one event type now: "final_response"
                        if event_name == "final_response":
                            logger.info(f"Received final_response from LangGraph for task {task_id}.")
                            
                            # Default to the simple TTS if no sequence is found
                            text_to_speak = data_json.get("final_text_for_tts")
                            ui_actions = data_json.get("final_ui_actions", [])
                            
                            sequence_action = next((a for a in ui_actions if a.get("action_type") == "EXECUTE_CONVERSATIONAL_SEQUENCE"), None)

                            if sequence_action:
                                # If we find a sequence, we orchestrate it.
                                sequence = sequence_action.get("parameters", {}).get("sequence", [])
                                logger.info(f"Orchestrating a sequence of {len(sequence)} actions.")
                                for step in sequence:
                                    if asyncio.current_task().cancelled(): break
                                    step_type = step.get("type")
                                    if step_type == "tts":
                                        await self.speak_text(step.get("content", ""))
                                    elif step_type == "listen":
                                        await self.handle_listen_step(step, caller_identity)
                                # Once the sequence is done, we can break from the SSE loop
                                # as we've handled the main payload.
                                break 
                            elif text_to_speak:
                                # If there's no sequence, just speak the main text.
                                await self.speak_text(text_to_speak)

                            # Handle any other non-sequence UI actions
                            other_ui_actions = [a for a in ui_actions if a.get("action_type") != "EXECUTE_CONVERSATIONAL_SEQUENCE"]
                            for action in other_ui_actions:
                                await trigger_client_ui_action(self._room, caller_identity, action.get("action_type", ""), action.get("parameters"))
                            
                            # Break after handling the main response
                            break
        except asyncio.CancelledError:
            logger.info(f"Task {task_id} was successfully cancelled.")
        except Exception as e:
            logger.error(f"Error during handling of task {task_id}: {e}", exc_info=True)
        finally:
            if task_id in self.active_tasks:
                del self.active_tasks[task_id]
            logger.info(f"Finished and cleaned up task {task_id}.")

    async def handle_listen_step(self, listen_step: dict, caller_identity: str):
        """
        Orchestrates a "listen" step by managing the user's audio track and
        waiting for a transcript from the VAD/STT pipeline.
        """
        timeout_s = listen_step.get("timeout_ms", 5000) / 1000.0
        logger.info(f"Agent is now 'listening' for a transcript from {caller_identity} for {timeout_s}s.")

        # Find the participant to listen to.
        participant = self._room.remote_participants.get(caller_identity)
        if not participant:
            logger.error(f"Cannot listen, participant {caller_identity} not found.")
            return

        # This is the core logic. We tell the AgentSession to expect speech from this user.
        # The VAD/STT pipeline for this user is now "active."
        self.agent_session.start_listening_to(participant)
        
        # We also send a UI action to the frontend to show a visual indicator.
        await trigger_client_ui_action(
            self._room,
            caller_identity,
            "START_LISTENING_VISUAL", # A new, simple UI action
            {"prompt_text": listen_step.get("prompt_if_silent", "I'm listening...")}
        )

        try:
            # Wait for the next transcript to arrive from the STT pipeline.
            # The agent's 'on_transcript' method will be called automatically by the AgentSession.
            # We need a way to get that transcript back here. An asyncio.Queue is perfect for this.
            
            # Assume self.transcript_queue = asyncio.Queue() was added in __init__
            transcript = await asyncio.wait_for(self.transcript_queue.get(), timeout=timeout_s)
            
            logger.info(f"Received transcript while listening: '{transcript}'")
            
            # We received a transcript, so we can stop listening.
            self.agent_session.stop_listening_to(participant)
            await trigger_client_ui_action(self._room, caller_identity, "STOP_LISTENING_VISUAL", {})

            # Now, trigger a new LangGraph task to handle the user's response.
            await self.trigger_langgraph_task(
                task_name="handle_student_response",
                json_payload=json.dumps({
                    "user_id": caller_identity, # Assuming participant identity is the user_id
                    "transcript": transcript,
                    "expected_intent": listen_step.get("expected_intent")
                }),
                caller_identity=caller_identity
            )

        except asyncio.TimeoutError:
            logger.info(f"Listen step timed out for {caller_identity}. No transcript received.")
            self.agent_session.stop_listening_to(participant)
            await trigger_client_ui_action(self._room, caller_identity, "STOP_LISTENING_VISUAL", {})
            
            # If there's a prompt for silence, speak it.
            if prompt_if_silent := listen_step.get("prompt_if_silent"):
                await self.speak_text(prompt_if_silent)

        finally:
            # Ensure we always stop listening in case of other errors.
            self.agent_session.stop_listening_to(participant)

    async def processing_loop(self):
        """The main non-blocking loop to dispatch tasks.""" 
        logger.info("Agent's main processing loop is running.")
        while True:
            task_name, json_payload, caller_identity, task_id = await self.task_queue.get()
            
            logger.info(f"Dequeued task {task_id} ('{task_name}'). Creating background handler.")
            
            # Create the background task and store it in our tracking dictionary.
            task = asyncio.create_task(
                self._handle_single_task(task_name, json_payload, caller_identity, task_id)
            )
            self.active_tasks[task_id] = task
            self.task_queue.task_done()

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
                yield current_event_name, json.loads(data_str)
                current_event_name = None
                current_event_data_lines = []


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
    rox_agent_instance._job_ctx = ctx
    rox_agent_instance._room = ctx.room
    
    main_agent_session = agents.AgentSession(  # Renamed for clarity
        stt=deepgram.STT(model="nova-2", language="multi"),  # nova-2 or nova-3
        llm=CustomLLMBridge,  # Pass agent instance
        tts=deepgram.TTS(model="aura-asteria-en", api_key=os.environ.get("DEEPGRAM_API_KEY")),
        vad=silero.VAD.load(),
        turn_detection=MultilingualModel(),
    )
    rox_agent_instance.agent_session = main_agent_session
    
    # --- RPC REGISTRATION (THE CRITICAL FIX) ---
    # This is where we register all the methods the frontend can call.
    
    agent_rpc_service = AgentInteractionService(agent_instance=rox_agent_instance)
    service_name = "rox.interaction.AgentInteraction"
    local_participant = ctx.room.local_participant
    
    logger.info("Registering all RPC handlers...")
    try:
        local_participant.register_rpc_method(f"{service_name}/InvokeAgentTask", agent_rpc_service.InvokeAgentTask)
        local_participant.register_rpc_method(f"{service_name}/HandleFrontendButton", agent_rpc_service.HandleFrontendButton)
        local_participant.register_rpc_method(f"{service_name}/NotifyPageLoadV2", agent_rpc_service.NotifyPageLoadV2)
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