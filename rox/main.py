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
    """Rox AI assistant with UI interaction capabilities."""
    def __init__(self, **kwargs):
        # Set default instructions if not provided, to satisfy Agent base class
        kwargs.setdefault(
            "instructions",
            "You are Rox, an AI assistant for students using the learning platform. You help students understand their learning status and guide them through their learning journey."
        )
        super().__init__(**kwargs)
        self.agent_session: Optional[agents.AgentSession] = None
        self._room: Optional[rtc.Room] = None
        self._job_ctx: Optional[JobContext] = None

    async def speak_text(self, text: str):
        if not self.agent_session or not text:
            return
        try:
            logger.info(f"Agent speaking: {text[:80]}...")
            await self.agent_session.say(text, add_to_chat_ctx=False)
        except Exception as e:
            logger.error(f"Error in speak_text: {e}", exc_info=True)

    # +++ NEW GENERIC TASK TRIGGER WITH CORRECTED SSE LOOP +++
    async def trigger_langgraph_task(self, task_name: str, json_payload: str, caller_identity: str):
        """
        A generic method to send any task to the LangGraph backend and stream the response
        back to the client. THIS IS THE NEW, CORRECT SSE CONSUMER.
        """
        logger.info(f"Agent triggering LangGraph task: '{task_name}' for user '{caller_identity}'.")
        
        try:
            langgraph_url = os.getenv("MY_CUSTOM_AGENT_URL")
            if not langgraph_url:
                raise ValueError("MY_CUSTOM_AGENT_URL is not set in environment.")
                
            streaming_endpoint = langgraph_url.replace("/process_interaction", "/invoke_task_streaming")
            
            request_body = {
                "task_name": task_name,
                "json_payload": json_payload  # Pass the payload as a JSON string
            }

            async with aiohttp.ClientSession() as http_session:
                async with http_session.post(streaming_endpoint, json=request_body) as response:
                    if response.status != 200:
                        err_text = await response.text()
                        logger.error(f"LangGraph returned an error: {response.status} - {err_text}")
                        return

                    logger.info("Connection to LangGraph established. Parsing SSE stream...")
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
                            data_json = json.loads(data_str)

                            # --- THIS IS THE CORRECTED LOGIC THAT WAS MISSING ---
                            if current_event_name == "streaming_text_chunk":
                                text_to_speak = data_json.get('streaming_text_chunk')
                                if text_to_speak:
                                    await self.speak_text(text_to_speak)

                            elif current_event_name == "final_ui_actions":
                                ui_actions = data_json.get('ui_actions', [])
                                logger.info(f"Processing {len(ui_actions)} UI actions from LangGraph.")
                                for action_data in ui_actions:
                                    await trigger_client_ui_action(
                                        room=self._room,
                                        client_identity=caller_identity,
                                        action_type=action_data.get("action_type"),
                                        parameters=action_data.get("parameters", {})
                                    )
                            
                            elif current_event_name == "stream_end":
                                logger.info(f"SSE stream ended from LangGraph: {data_json.get('message')}")
                            
                            else:
                                # This will catch 'stream_start' and any others without breaking the loop
                                logger.debug(f"Received SSE event '{current_event_name}' with no specific handler.")
                            
                            # Reset for the next event
                            current_event_name = None
                            current_event_data_lines = []
        
        except Exception as e:
            logger.error(f"Error in trigger_langgraph_task for task '{task_name}': {e}", exc_info=True)


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
        tts=deepgram.TTS,
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

    # Keep the agent alive to listen for events and RPC calls
    logging.info("Agent is running and waiting for events...")
    try:
        await asyncio.Event().wait()  # Wait indefinitely
    except asyncio.CancelledError:
        logging.info("Agent entrypoint task cancelled.")
    finally:
        logging.info("Agent shutting down.")
    # +++ END OF NEW HANDSHAKE LOGIC +++

    logger.info("Rox agent fully operational. Starting main VAD session...")
    await main_agent_session.start(room=ctx.room, agent=rox_agent_instance)

    await asyncio.Event().wait()  # Keep agent alive
    
    logger.info(f"Agent job for room '{ctx.room.name}' has ended.")

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