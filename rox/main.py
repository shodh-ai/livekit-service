#!/usr/bin/env python3
"""
Rox Assistant LiveKit Agent - Corrected and Refactored with Sentence-Level Event Splitting
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
from urllib.parse import urlparse, urlunparse
from livekit.plugins import noise_cancellation
from livekit.plugins import deepgram, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import PromptTemplate

# Add project root to path for clean imports
project_root = Path(__file__).resolve().parent
sys.path.insert(0, str(project_root))

from dotenv import load_dotenv
from typing import Optional, Dict, Any

# Third-party imports
import aiohttp
from livekit import rtc, agents
from livekit.agents import Agent, JobContext, RoomInputOptions, WorkerOptions

# +++ NLTK IMPORT FOR SENTENCE TOKENIZATION +++
import nltk
from nltk.tokenize import sent_tokenize

# Local application imports
from generated.protos import interaction_pb2
from rpc_services import AgentInteractionService
from utils.ui_action_factory import (
    build_ui_action_request,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# +++ NLTK DATA SETUP (CORRECTED) +++
# Download the 'punkt' tokenizer data if it's not already present.
# This is required for sentence splitting.
try:
    nltk.data.find("tokenizers/punkt")
except LookupError:  # <-- THIS IS THE FIX
    logger.info("NLTK 'punkt' tokenizer not found. Downloading...")
    nltk.download("punkt", quiet=True)
    logger.info("'punkt' downloaded successfully.")


# --- Environment Loading and Validation ---
load_dotenv(dotenv_path=project_root / ".env")
# ...

# Define the RPC method name that the agent calls on the client
CLIENT_RPC_FUNC_PERFORM_UI_ACTION = "rox.interaction.ClientSideUI/PerformUIAction"


async def trigger_client_ui_action(
    room: rtc.Room,
    client_identity: str,
    action_type: str,
    parameters: Optional[Dict[str, Any]] = None,
) -> Optional[interaction_pb2.ClientUIActionResponse]:
    """
    Builds and sends a UI action RPC to a client using the action factory.
    """
    if not client_identity:
        logger.error(
            "B2F RPC: Client identity was not provided. Cannot send UI action."
        )
        return None

    try:
        request_pb = build_ui_action_request(action_type, parameters or {})
        request_pb.request_id = f"ui-{uuid.uuid4().hex[:8]}"
        rpc_method_name = f"rox.interaction.ClientSideUI/PerformUIAction"
        payload_bytes = request_pb.SerializeToString()
        base64_encoded_payload = base64.b64encode(payload_bytes).decode("utf-8")
        response_payload_str = await room.local_participant.perform_rpc(
            destination_identity=client_identity,
            method=rpc_method_name,
            payload=base64_encoded_payload,
        )
        response_bytes = base64.b64decode(response_payload_str)
        response_pb = interaction_pb2.ClientUIActionResponse()
        response_pb.ParseFromString(response_bytes)
        return response_pb

    except Exception as e:
        logger.error(
            f"Failed to send UI action RPC to '{client_identity}': {e}", exc_info=True
        )
        return None


class RoxAgent(Agent):
    """Rox AI assistant with UI interaction capabilities."""

    def __init__(self, **kwargs):
        kwargs.setdefault(
            "instructions",
            "You are Rox, an AI assistant for students using the learning platform. You help students understand their learning status and guide them through their learning journey.",
        )
        super().__init__(**kwargs)
        self.agent_session: Optional[agents.AgentSession] = None
        self._room: Optional[rtc.Room] = None
        self._job_ctx: Optional[JobContext] = None
        self.task_queue: asyncio.Queue[tuple] = asyncio.Queue()
        self.user_has_doubt: Optional[bool] = False
        self.spoken_text: Optional[str] = ""
        self.doubt_prompt = self._create_doubt_prompt()
        self.doubt_llm = self._initialize_llm()

    def _initialize_llm(self):
        """Creates and returns the ChatGoogleGenerativeAI instance."""
        logger.info("Initializing Google AI LLM...")
        if not os.getenv("GOOGLE_API_KEY"):
            logger.error(
                "GOOGLE_API_KEY not found in environment variables. LLM will not be available."
            )
            return None

        return ChatGoogleGenerativeAI(
            model="gemini-2.0-flash",
            api_key=os.getenv("GOOGLE_API_KEY"),
            temperature=0.7,
        )

    def _create_doubt_prompt(self) -> PromptTemplate:
        """Creates and returns a PromptTemplate for answering doubts."""
        return PromptTemplate(
            template="""You are Rox, a helpful AI assistant for students.
A student was listening to you explain something and has a question.
Be clear, concise, and helpful.

Here is the context of what you were saying before the student interrupted:
---
{agent_context}
---

Here is the student's question:
---
{user_doubt}
---

Your helpful answer:""",
            input_variables=["agent_context", "user_doubt"],
        )

    async def speak_text(self, text: str):
        if self.agent_session and text:
            try:
                await self.agent_session.say(text, allow_interruptions=True)
            except Exception as e:
                logger.error(f"Error during agent_session.say(): {e}", exc_info=True)
        else:
            logger.warning(
                "Agent session not available or text is empty, cannot speak."
            )

    async def trigger_langgraph_task(
        self, task_name: str, json_payload: str, caller_identity: str
    ):
        await self.task_queue.put((task_name, json_payload, caller_identity))

    async def task_delegator_loop(self):
        while True:
            try:
                task_name, json_payload, caller_identity = await self.task_queue.get()
                asyncio.create_task(
                    self._execute_task_pipeline(
                        task_name, json_payload, caller_identity
                    )
                )
            except Exception as e:
                logger.error(
                    f"FATAL: Error in the main task_delegator_loop: {e}", exc_info=True
                )

    async def parse_sse_stream(self, response):
        current_event_name = None
        current_event_data_lines = []
        async for line_bytes in response.content:
            line = line_bytes.decode("utf-8").strip()
            if line.startswith("event:"):
                current_event_name = line[len("event:") :].strip()
            elif line.startswith("data:"):
                current_event_data_lines.append(line[len("data:") :].strip())
            elif not line and current_event_name:
                data_str = "\n".join(current_event_data_lines)
                try:
                    data_json = json.loads(data_str)
                    yield {"event_name": current_event_name, "data": data_json}
                except json.JSONDecodeError:
                    logger.warning(f"Could not decode SSE data as JSON: {data_str}")
                current_event_name = None
                current_event_data_lines = []

    async def _execute_task_pipeline(
        self, task_name: str, json_payload: str, caller_identity: str
    ):
        event_queue = asyncio.Queue()
        try:
            await asyncio.gather(
                self._sse_event_producer(task_name, json_payload, event_queue),
                self._event_queue_consumer(caller_identity, event_queue),
            )
        except Exception as e:
            logger.error(
                f"Error in task pipeline for '{task_name}': {e}", exc_info=True
            )

    async def _sse_event_producer(
        self, task_name: str, json_payload: str, event_queue: asyncio.Queue
    ):
        """
        Producer: Connects to the backend, retrieves SSE events, breaks text
        chunks into sentences, and puts granular events onto the queue.
        """
        langgraph_url = os.getenv("MY_CUSTOM_AGENT_URL")
        if not langgraph_url:
            logger.error("MY_CUSTOM_AGENT_URL is not set. Cannot contact LangGraph.")
            await event_queue.put(None)
            return

        try:
            parsed_url = urlparse(langgraph_url)
            endpoint = urlunparse(parsed_url._replace(path="/invoke_task_streaming"))
            request_body = {"task_name": task_name, "json_payload": json_payload}

            async with aiohttp.ClientSession() as http_session:
                async with http_session.post(endpoint, json=request_body) as response:
                    if response.status != 200:
                        logger.error(
                            f"LangGraph error for task '{task_name}': {response.status}"
                        )
                        return

                    # Process the stream of events from the backend
                    async for event in self.parse_sse_stream(response):
                        event_name = event.get("event_name")
                        data = event.get("data", {})

                        # If it's a text chunk, split it into sentences
                        if event_name == "streaming_text_chunk":
                            full_text = data.get("streaming_text_chunk", "")
                            if not full_text:
                                continue

                            sentences = sent_tokenize(full_text)
                            # Queue each sentence as an individual event
                            for sentence in sentences:
                                if sentence.strip():
                                    sentence_event = {
                                        "event_name": "streaming_text_chunk",
                                        "data": {
                                            "streaming_text_chunk": sentence.strip()
                                        },
                                    }
                                    await event_queue.put(sentence_event)
                        else:
                            await event_queue.put(event)

        except Exception as e:
            logger.error(f"Producer error for task '{task_name}': {e}", exc_info=True)
        finally:
            await event_queue.put(None)

    async def _event_queue_consumer(
        self, caller_identity: str, event_queue: asyncio.Queue
    ):
        logger.info("CONSUMER-LIFECYCLE: Consumer started.")
        while True:
            logger.info("CONSUMER-LOOP: Top of loop.")
            if self.user_has_doubt:
                logger.warning(
                    "CONSUMER-LOOP: `user_has_doubt` is True. Entering handler."
                )
                await self._handle_user_doubt(caller_identity)
                logger.warning(
                    "CONSUMER-LOOP: Returned from handler. Executing 'continue'."
                )
                continue  # This will jump back to the top of the while loop

            logger.info("CONSUMER-LOOP: Waiting for an event from the queue...")
            event = await event_queue.get()
            logger.info(
                f"CONSUMER-LOOP: Got event from queue: {event and event.get('event_name')}"
            )

            if event is None:
                logger.warning("CONSUMER-LOOP: Received 'None' event. Breaking loop.")
                break

            try:
                event_name = event.get("event_name")
                data = event.get("data", {})

                if event_name == "streaming_text_chunk":
                    text_to_speak = data.get("streaming_text_chunk", "")
                    logger.info(
                        f"CONSUMER-ACTION: Speaking chunk: '{text_to_speak[:30]}...'"
                    )
                    self.spoken_text += text_to_speak
                    await self.speak_text(text_to_speak)

                elif event_name == "final_ui_actions":
                    logger.info("CONSUMER-ACTION: Processing UI actions.")
                    ui_actions = data.get("ui_actions", [])
                    for action_data in ui_actions:
                        asyncio.create_task(
                            trigger_client_ui_action(
                                room=self._room,
                                client_identity=caller_identity,
                                action_type=action_data.get("action_type"),
                                parameters=action_data.get("parameters", {}),
                            )
                        )

            except Exception as e:
                logger.error(
                    f"CONSUMER-LOOP: Error while processing event: {e}", exc_info=True
                )
            finally:
                event_queue.task_done()

        logger.warning("CONSUMER-LIFECYCLE: Consumer has exited its loop.")

    async def pause_main_activity(self):
        self.user_has_doubt = True

    async def _handle_user_doubt(self, caller_identity: str):
        await self.speak_text("Please ask your question.")

        try:
            await trigger_client_ui_action(
                room=self._room,
                client_identity=caller_identity,
                action_type="START_DOUBT_LISTENING_SESSION",
            )

            participant = self._room.remote_participants.get(caller_identity)
            if not participant:
                logger.error(f"DOUBT-FLOW: Participant '{caller_identity}' not found.")
                return

            for _ in range(10):
                for track_pub in participant.track_publications.values():
                    if track_pub.name == "user-doubt-audio" and track_pub.track:
                        doubt_track = track_pub.track
                        break
                if doubt_track:
                    break
                await asyncio.sleep(0.5)

            if not doubt_track:
                logger.error(
                    "DOUBT-FLOW: Timed out waiting for 'user-doubt-audio' track."
                )
                return

            final_transcript = await asyncio.wait_for(
                self._listen_and_transcribe_doubt(doubt_track),
                timeout=30,
            )

            logger.info("--- USER DOUBT CAPTURED ---")
            logger.info(f"Agent's spoken text before doubt: '{self.spoken_text}'")
            logger.info(f"User's transcribed doubt: '{final_transcript}'")
            logger.info("--------------------------")

            if not self.doubt_llm:
                logger.error(
                    "DOUBT-FLOW: LLM is not initialized. Cannot generate response."
                )
                await self.speak_text(
                    "I'm sorry, I am unable to answer questions at the moment."
                )
                return

            logger.info("DOUBT-FLOW: Generating response from Google AI...")
            # Construct the chain from the pre-initialized components
            chain = self.doubt_prompt | self.doubt_llm
            chain_input = {
                "agent_context": self.spoken_text,
                "user_doubt": final_transcript,
            }
            response = await chain.ainvoke(chain_input)
            response_text = response.content

            logger.info(f"DOUBT-FLOW: AI Generated Response: '{response_text}'")
            await self.speak_text(response_text)

        except Exception as e:
            logger.error(
                f"DOUBT-FLOW: Error starting doubt listening session: {e}",
                exc_info=True,
            )
        finally:
            await trigger_client_ui_action(
                room=self._room,
                client_identity=caller_identity,
                action_type="END_DOUBT_LISTENING_SESSION",
            )
            await self.speak_text("Hope that clarified it.")
            await asyncio.sleep(1)
            self.user_has_doubt = False

    async def _listen_and_transcribe_doubt(
        self,
        doubt_track: rtc.Track,
    ) -> str:
        """
        Listens to a specific audio track and transcribes the speech.

        This function is fully self-contained. It creates temporary instances
        of both the STT and VAD engines to avoid interfering with the main
        agent session's components.
        """
        logger.info(
            "DOUBT-FLOW: Creating temporary STT and VAD engines for transcription."
        )
        # Create a new, isolated STT engine for this task.
        stt_engine = deepgram.STT(model="nova-2", language="multi")
        stt_stream = stt_engine.stream()

        # Create a new, isolated VAD engine configured for this task.
        vad_engine = silero.VAD.load(min_silence_duration=3.0)
        vad_stream = vad_engine.stream()

        audio_stream = rtc.AudioStream(doubt_track)
        final_transcript = ""
        stop_event = asyncio.Event()

        async def audio_pump():
            """Pushes audio frames to STT and VAD until stop_event is set."""
            try:
                async for frame_event in audio_stream:
                    if stop_event.is_set():
                        break
                    try:
                        vad_stream.push_frame(frame_event.frame)
                        stt_stream.push_frame(frame_event.frame)
                    except Exception as e:
                        logger.debug(f"DOUBT-FLOW: Error pushing frame: {e}")
            finally:
                logger.info("DOUBT-FLOW: Audio pump finished.")
                try:
                    await stt_stream.aclose()
                except Exception:
                    pass
                try:
                    await vad_stream.aclose()
                except Exception:
                    pass

        async def vad_monitor():
            """Sets stop_event when the VAD detects the end of speech."""
            try:
                async for event in vad_stream:
                    if event.type == agents.vad.VADEventType.END_OF_SPEECH:
                        logger.info("DOUBT-FLOW: VAD detected end of speech.")
                        stop_event.set()
                        break
            finally:
                logger.info("DOUBT-FLOW: VAD monitor finished.")

        async def transcript_collector():
            """Collects final transcription results from the STT stream."""
            nonlocal final_transcript
            try:
                async for event in stt_stream:
                    if event.type == agents.stt.SpeechEventType.FINAL_TRANSCRIPT:
                        if event.alternatives and event.alternatives[0].text:
                            final_transcript += event.alternatives[0].text + " "
            finally:
                logger.info("DOUBT-FLOW: Transcript collector finished.")

        try:
            await asyncio.gather(audio_pump(), vad_monitor(), transcript_collector())
        except Exception as e:
            logger.error(f"DOUBT-FLOW: Error during transcription: {e}", exc_info=True)

        return final_transcript.strip()


async def entrypoint(ctx: JobContext):
    logger.info(f"Agent job starting for room '{ctx.room.name}'.")

    try:
        await ctx.connect()
        logger.info(f"Successfully connected to LiveKit room '{ctx.room.name}'")
    except Exception as e:
        logger.error(f"Failed to connect to LiveKit room: {e}", exc_info=True)
        return

    rox_agent_instance = RoxAgent()
    rox_agent_instance._job_ctx = ctx
    rox_agent_instance._room = ctx.room

    main_agent_session = agents.AgentSession(
        tts=deepgram.TTS(
            model="aura-asteria-en", api_key=os.environ.get("DEEPGRAM_API_KEY")
        ),
    )
    rox_agent_instance.agent_session = main_agent_session

    agent_rpc_service = AgentInteractionService(agent_instance=rox_agent_instance)
    service_name = "rox.interaction.AgentInteraction"
    local_participant = ctx.room.local_participant

    logger.info("Registering all RPC handlers...")
    try:
        local_participant.register_rpc_method(
            f"{service_name}/InvokeAgentTask", agent_rpc_service.InvokeAgentTask
        )
        local_participant.register_rpc_method(
            f"{service_name}/HandleFrontendButton",
            agent_rpc_service.HandleFrontendButton,
        )
        local_participant.register_rpc_method(
            f"{service_name}/NotifyPageLoadV2", agent_rpc_service.NotifyPageLoadV2
        )
        local_participant.register_rpc_method(
            f"{service_name}/TestPing", agent_rpc_service.TestPing
        )
        local_participant.register_rpc_method(
            f"{service_name}/HandlePushToTalk", agent_rpc_service.HandlePushToTalk
        )
        logging.info("agent rpc handlers registered")
    except Exception as e:
        logger.error(f"Failed to register one or more RPC handlers: {e}", exc_info=True)
        return

    # Handshake logic
    try:
        await asyncio.sleep(1)
        if len(ctx.room.remote_participants) > 0:
            first_participant_identity = list(ctx.room.remote_participants.keys())[0]
            logging.info(
                f"Sending 'agent_ready' handshake to {first_participant_identity}."
            )
            handshake_payload = json.dumps(
                {
                    "type": "agent_ready",
                    "agent_identity": ctx.room.local_participant.identity,
                }
            )
            await ctx.room.local_participant.publish_data(
                payload=handshake_payload,
                destination_identities=[first_participant_identity],
            )
        else:
            logging.warning("No participants in the room after 1s, skipping handshake.")
    except Exception as e:
        logging.error(f"Failed to send 'agent_ready' handshake: {e}", exc_info=True)

    logger.info(
        "Rox agent fully operational. Starting main session and task delegator loop..."
    )

    await asyncio.gather(
        main_agent_session.start(room=ctx.room, agent=rox_agent_instance),
        rox_agent_instance.task_delegator_loop(),
    )


if __name__ == "__main__":
    try:
        agents.cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
    except Exception as e:
        logger.error(f"Failed to start LiveKit Agent CLI: {e}", exc_info=True)
