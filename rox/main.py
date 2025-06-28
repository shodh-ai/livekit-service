# main.py (FINAL, SIMPLIFIED, AND CORRECT VERSION)

import os
import sys
import logging
import asyncio
import json
import uuid
import base64
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from typing import Dict, Optional, Any

# Add project root to path for clean imports
project_root = Path(__file__).resolve().parent
sys.path.insert(0, str(project_root))

from dotenv import load_dotenv
import aiohttp
from livekit import rtc, agents
from livekit.agents import Agent, JobContext, WorkerOptions
from livekit.plugins import deepgram, silero

from generated.protos import interaction_pb2
from rpc_services import AgentInteractionService
from utils.ui_action_factory import build_ui_action_request

# --- Configuration & Logging ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
load_dotenv(dotenv_path=project_root / ".env")


async def trigger_client_ui_action(
    room: rtc.Room,
    client_identity: str,
    action_type: str,
    parameters: Optional[Dict[str, Any]] = None,
) -> None:
    """Builds and sends a UI action RPC to a client using the action factory."""
    if not client_identity or not action_type:
        return

    try:
        request_pb = build_ui_action_request(action_type, parameters or {})
        request_pb.request_id = f"ui-{uuid.uuid4().hex[:8]}"
        rpc_method_name = "rox.interaction.ClientSideUI/PerformUIAction"
        payload_bytes = request_pb.SerializeToString()
        base64_encoded_payload = base64.b64encode(payload_bytes).decode("utf-8")

        logger.info(f"Sending RPC '{rpc_method_name}' to '{client_identity}'. Action: {action_type}")
        await room.local_participant.perform_rpc(
            destination_identity=client_identity,
            method=rpc_method_name,
            payload=base64_encoded_payload,
        )
    except Exception as e:
        logger.error(f"Failed to send UI action RPC to '{client_identity}': {e}", exc_info=True)


class RoxAgent(Agent):
    """
    The final, event-driven implementation of the Rox Agent. It reacts to transcripts
    and executes command sequences from LangGraph.
    """
    def __init__(self, instructions: str, **kwargs):
        super().__init__(instructions=instructions, **kwargs)
        self.agent_session: Optional[agents.AgentSession] = None
        self._room: Optional[rtc.Room] = None
        # The task queue serializes incoming requests to LangGraph.
        self.task_queue: asyncio.Queue[tuple] = asyncio.Queue()

    async def on_transcript(self, transcript: str, participant: rtc.Participant, is_final: bool):
        """
        This method is the single, reliable entry point for all user speech.
        It is called automatically by the LiveKit AgentSession.
        """
        if is_final and transcript:
            logger.info(f"AGENT ON_TRANSCRIPT received: '{transcript}' from {participant.identity}")
            # When the user speaks, we create a new task for LangGraph to handle it.
            await self.trigger_langgraph_task(
                task_name="handle_student_response",
                json_payload=json.dumps({
                    "user_id": participant.identity,
                    "transcript": transcript
                }),
                caller_identity=participant.identity
            )

    async def speak_text(self, text: str):
        """A simple, non-blocking, and interruptible TTS call."""
        if self.agent_session and text:
            logger.info(f"Speaking: '{text[:50]}...'")
            await self.agent_session.say(text, allow_interruptions=True)

    async def trigger_langgraph_task(self, task_name: str, json_payload: str, caller_identity: str):
        """The single, simple entry point for starting any new graph execution."""
        logger.info(f"Queueing LangGraph task '{task_name}'.")
        await self.task_queue.put((task_name, json_payload, caller_identity))

    async def _handle_single_task(self, task_name: str, json_payload: str, caller_identity: str):
        """
        The orchestrator. It runs a LangGraph task and executes the returned commands.
        """
        logger.info(f"Processing task '{task_name}'.")
        try:
            # We use a non-streaming endpoint because our nodes now return a single, complete package.
            endpoint = urlunparse(urlparse(os.getenv("MY_CUSTOM_AGENT_URL"))._replace(path="/invoke_task_streaming"))
            request_body = {"task_name": task_name, "json_payload": json_payload}
            
            async with aiohttp.ClientSession() as session:
                async with session.post(endpoint, json=request_body) as response:
                    if response.status != 200:
                        raise Exception(f"LangGraph service error: {response.status} - {await response.text()}")

                    final_response = None
                    # Process the server-sent event stream
                    async for line in response.content:
                        # SSE messages are prefixed with 'data: '
                        if line.strip().startswith(b'data:'):
                            data_str = line.strip()[5:].strip()
                            if not data_str:
                                continue
                            
                            try:
                                event_data = json.loads(data_str)
                                # The final, complete output from our graph is what we care about.
                                # We identify it by the presence of our specific output keys.
                                if "final_text_for_tts" in event_data or "final_ui_actions" in event_data:
                                    final_response = event_data
                                    logger.info(f"Extracted final response from stream: {final_response}")
                            except json.JSONDecodeError:
                                logger.warning(f"Could not decode JSON from stream line: {data_str}")
                    
                    if final_response is None:
                        raise Exception("Stream completed without finding the final graph output.")
            
            logger.info(f"Received final response from LangGraph for task '{task_name}'.")

            text_to_speak = final_response.get("final_text_for_tts")
            ui_actions = final_response.get("final_ui_actions", [])

            # Speak first, if there's text.
            if text_to_speak:
                await self.speak_text(text_to_speak)
            
            # Then, process all UI actions sequentially.
            for action in ui_actions:
                action_type = action.get("action_type")
                parameters = action.get("parameters", {})
                await trigger_client_ui_action(self._room, caller_identity, action_type, parameters)

        except Exception as e:
            logger.error(f"Error during handling of task '{task_name}': {e}", exc_info=True)
            # Optionally, speak an error message to the user.
            await self.speak_text("I'm sorry, I encountered a technical issue. Let's try that again.")

    async def processing_loop(self):
        """The non-blocking loop that dispatches tasks from the queue."""
        logger.info("Agent's main processing loop is running.")
        while True:
            try:
                task_name, json_payload, caller_identity = await self.task_queue.get()
                asyncio.create_task(self._handle_single_task(task_name, json_payload, caller_identity))
                self.task_queue.task_done()
            except Exception as e:
                logger.error(f"CRITICAL error in processing_loop: {e}", exc_info=True)


async def entrypoint(ctx: JobContext):
    logger.info(f"Agent job starting for room '{ctx.room.name}'.")
    await ctx.connect()

    # 1. Create the agent instance.
    rox_agent_instance = RoxAgent(instructions="You are Rox...")
    rox_agent_instance._room = ctx.room

    # 2. Create the AgentSession for STT/TTS.
    main_agent_session = agents.AgentSession(
        stt=deepgram.STT(model="nova-2", language="multi"),
        tts=deepgram.TTS(model="aura-asteria-en", api_key=os.getenv("DEEPGRAM_API_KEY")),
        vad=silero.VAD.load(),
    )
    rox_agent_instance.agent_session = main_agent_session

    # 3. Register individual RPC methods from the service.
    agent_rpc_service = AgentInteractionService(agent=rox_agent_instance)
    rpc_topic_prefix = "rox.interaction.AgentInteraction"
    ctx.room.local_participant.register_rpc_method(
        f"{rpc_topic_prefix}/InvokeAgentTask", agent_rpc_service.InvokeAgentTask
    )
    ctx.room.local_participant.register_rpc_method(
        f"{rpc_topic_prefix}/RequestInterrupt", agent_rpc_service.RequestInterrupt
    )
    ctx.room.local_participant.register_rpc_method(
        f"{rpc_topic_prefix}/TestPing", agent_rpc_service.TestPing
    )
    logger.info("AgentInteractionService RPC methods registered.")

    # 4. Run all long-running tasks concurrently using asyncio.gather.

    # Task 1: The agent's own processing loop for handling LangGraph tasks.
    processing_task = rox_agent_instance.processing_loop()

    # Task 2: The LiveKit AgentSession's main loop for handling VAD/STT/TTS.
    session_task = main_agent_session.start(room=ctx.room, agent=rox_agent_instance)

    # Task 3: A new, robust task to send the "agent_ready" handshake.
    async def send_ready_handshake():
        # Wait up to 10 seconds for a participant to join.
        for _ in range(10):
            if len(ctx.room.remote_participants) > 0:
                participant_identity = list(ctx.room.remote_participants.keys())[0]
                logger.info(f"Participant '{participant_identity}' found. Sending 'agent_ready' handshake.")
                handshake_payload = json.dumps({
                    "type": "agent_ready",
                    "agent_identity": ctx.room.local_participant.identity
                })
                await ctx.room.local_participant.publish_data(
                    payload=handshake_payload,
                    destination_identities=[participant_identity],
                )
                return # Exit the function once sent
            await asyncio.sleep(1)
        
        logger.warning("Handshake timed out: No remote participants joined after 10 seconds.")

    handshake_task = send_ready_handshake()

    logger.info("Rox agent is operational. Running all tasks concurrently.")

    # This will run all three tasks forever, until one of them fails.
    await asyncio.gather(processing_task, session_task, handshake_task)

if __name__ == "__main__":
    try:
        agents.cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
    except Exception as e:
        logger.error(f"Failed to start LiveKit Agent CLI: {e}", exc_info=True)