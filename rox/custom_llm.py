# custom_llm.py

import os
import json
import logging
import time
import httpx  # Replaced aiohttp with httpx
from typing import AsyncIterable, Optional, TYPE_CHECKING, List, Dict, Any
import uuid
import asyncio

if TYPE_CHECKING:
    from .main import RoxAgent # Import RoxAgent for type hinting to avoid circular dependency
from contextlib import asynccontextmanager

from livekit.agents.llm import LLM, ChatContext, ChatMessage, ChatRole, ChatChunk, ChoiceDelta
import uuid

logger = logging.getLogger(__name__)

# Get the URL of your custom backend agent from environment variables
MY_CUSTOM_AGENT_URL = os.getenv("MY_CUSTOM_AGENT_URL", "http://localhost:8080/process_interaction") # Default non-streaming URL
MY_FASTAPI_URL_STREAMING = os.getenv("MY_CUSTOM_AGENT_URL_STREAMING", "http://localhost:8080/process_interaction_streaming") # New Streaming URL
MY_LANGGRAPH_TASK_URL = os.getenv("MY_LANGGRAPH_TASK_URL", "http://localhost:8080/invoke_task_streaming")

class CustomLLMBridge(LLM):
    """
    A custom LLM component that bridges to an external backend script/service.
    """
    def __init__(self,
                 agent_url: str = MY_CUSTOM_AGENT_URL,
                 page_name: Optional[str] = None,
                 rox_agent_ref: Optional['RoxAgent'] = None): # Add rox_agent_ref
        super().__init__()
        if not agent_url: # This refers to the base URL, streaming URL is separate
            logger.warning("MY_CUSTOM_AGENT_URL is not set. Ensure MY_FASTAPI_URL_STREAMING is set for streaming.")
        self._agent_url = agent_url # Keep for potential non-streaming use or reference
        self._streaming_agent_url = MY_FASTAPI_URL_STREAMING # Store the streaming URL
        self._page_name = page_name
        self._rox_agent_ref = rox_agent_ref
        if self._rox_agent_ref:
            logger.info(f"CustomLLMBridge initialized with RoxAgent reference. Streaming URL: {self._streaming_agent_url} for page: {self._page_name}")
        else:
            logger.warning(f"CustomLLMBridge initialized WITHOUT RoxAgent reference. Context data will not be available. Streaming URL: {self._streaming_agent_url} for page: {self._page_name}")
        self._task_url = MY_LANGGRAPH_TASK_URL # Add this
        logger.info(f"LangGraph task streaming URL: {self._task_url}")

    def add_user_token(self, user_token: str, user_id: str):
        self._user_token = user_token
        self._user_id = user_id
        logger.info(f"User token added to CustomLLMBridge: {self._user_token} and user_id: {self._user_id}")

    def chat(self, *, chat_ctx: ChatContext = None, tools = None, tool_choice = None):
        return self._chat_context_manager(chat_ctx, tools, tool_choice)

    async def invoke_task_streaming(self, task_name: str, json_payload_str: str) -> AsyncIterable[tuple[str, Any]]:
        """
        Calls the generic LangGraph task endpoint and streams back events.
        """
        payload = {
            "task_name": task_name,
            "json_payload": json_payload_str,
            # Include agent context if available
            "agent_context": self._rox_agent_ref._latest_student_context if self._rox_agent_ref else {}
        }
        
        logger.info(f"Invoking LangGraph task '{task_name}' at {self._task_url}")

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream("POST", self._task_url, json=payload) as response:
                    response.raise_for_status()
                    
                    current_event_type = None
                    current_event_data_lines = []

                    async for line_bytes in response.aiter_bytes():
                        line = line_bytes.decode('utf-8').strip()

                        if line.startswith("event:"):
                            current_event_type = line.split(":", 1)[1].strip()
                            current_event_data_lines = [] # Reset for new event data
                        elif line.startswith("data:"):
                            current_event_data_lines.append(line.split(":", 1)[1].strip())
                        elif not line and current_event_type:
                            # End of event, process it
                            data_str = "".join(current_event_data_lines)
                            try:
                                data_obj = json.loads(data_str)
                                yield (current_event_type, data_obj)
                            except json.JSONDecodeError:
                                # If data is not JSON, yield as raw string
                                yield (current_event_type, data_str)
                            
                            # Reset for next event
                            current_event_type = None
                            current_event_data_lines = []

        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error calling LangGraph task endpoint: {e.response.status_code} - {e.response.text}")
            yield ("error", {"message": f"Backend Error: {e.response.status_code}"})
        except Exception as e:
            logger.error(f"Error in invoke_task_streaming: {e}", exc_info=True)
            yield ("error", {"message": "An unexpected error occurred while contacting the backend."})


    async def _call_fastapi_streaming_sse(self, fastapi_payload: dict, tts_pusher_function) -> Optional[List[Dict[str, Any]]]:
        """
        Calls the streaming FastAPI endpoint and processes the Server-Sent Events.
        Pushes TTS text chunks using tts_pusher_function and returns collected UI actions.
        """
        collected_ui_actions = []
        full_response_for_log = [] # For logging the full conversation text

        try:
            async with httpx.AsyncClient(timeout=120.0) as client: # Increased timeout
                async with client.stream("POST", self._streaming_agent_url, json=fastapi_payload) as response:
                    response.raise_for_status()
                    logger.info(f"Successfully connected to streaming endpoint: {self._streaming_agent_url}")
                    
                    current_event_type = None
                    current_event_data_lines = []

                    async for line_bytes in response.aiter_bytes():
                        line = line_bytes.decode('utf-8').strip()
                        # logger.debug(f"Raw SSE line: {line}") # Verbose

                        if not line: # Empty line signifies end of an event
                            if current_event_type and current_event_data_lines:
                                data_str = "".join(current_event_data_lines)
                                # logger.debug(f"Processing event: type='{current_event_type}', data='{data_str}'")
                                try:
                                    data_obj = json.loads(data_str)
                                    
                                    if current_event_type == "text_chunk":
                                        text_chunk_content = data_obj.get("text", "")
                                        if text_chunk_content:
                                            await tts_pusher_function(text_chunk_content)
                                            full_response_for_log.append(text_chunk_content)
                                            # logger.debug(f"Pushed TTS chunk: '{text_chunk_content}'")
                                    elif current_event_type == "ui_actions":
                                        ui_actions_content = data_obj.get("actions")
                                        if ui_actions_content:
                                            collected_ui_actions.extend(ui_actions_content)
                                            logger.info(f"Collected UI actions: {ui_actions_content}")
                                    elif current_event_type == "stream_end":
                                        logger.info(f"Stream ended by server: {data_obj.get('message')}")
                                        break # Exit loop on stream_end event
                                    elif current_event_type == "error":
                                        logger.error(f"Error event from stream: {data_obj.get('message')}")
                                        # Potentially raise an error or handle gracefully
                                        break 
                                    # else:
                                        # logger.debug(f"Ignoring SSE event type: {current_event_type}")

                                except json.JSONDecodeError:
                                    logger.error(f"Failed to decode stream JSON data: {data_str} for event: {current_event_type}")
                                except Exception as e_proc:
                                    logger.error(f"Error processing event data: {e_proc}", exc_info=True)
                            
                            current_event_type = None
                            current_event_data_lines = []
                            continue

                        if line.startswith("event:"):
                            current_event_type = line[len("event:"):].strip()
                        elif line.startswith("data:"):
                            current_event_data_lines.append(line[len("data:"):].strip())
                        # else:
                            # logger.debug(f"Ignoring non-SSE line: {line}")
            
            logger.info(f"Streaming finished. Full response for log: {''.join(full_response_for_log)}")
            return collected_ui_actions

        except httpx.RequestError as e:
            logger.error(f"HTTP request error calling streaming endpoint {self._streaming_agent_url}: {e}")
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP status error from streaming endpoint {self._streaming_agent_url}: {e.response.status_code} - {e.response.text}")
        except Exception as e:
            logger.error(f"An unexpected error occurred during streaming from {self._streaming_agent_url}: {e}", exc_info=True)
        
        return collected_ui_actions # Return any actions collected before an error, or empty

    @asynccontextmanager
    async def _chat_context_manager(self, chat_ctx: ChatContext, tools, tool_choice):
        try:
            async def response_generator():
                if not chat_ctx:
                    logger.warning("No chat context provided")
                    yield ChatChunk(id=str(uuid.uuid4()), delta=ChoiceDelta(role='assistant', content=""))
                    return

                # --- [Existing logic for extracting messages and transcript - largely unchanged] ---
                # (Copied from your provided context, ensure this part is correct for your ChatContext structure)
                logger.debug(f"CustomLLMBridge received chat context of type: {type(chat_ctx)}")
                try:
                    if hasattr(chat_ctx, '_items'): messages = getattr(chat_ctx, '_items')
                    elif hasattr(chat_ctx, 'items') and callable(getattr(chat_ctx, 'items')): messages = list(chat_ctx.items())
                    elif hasattr(chat_ctx, '__iter__'): messages = list(chat_ctx)
                    elif hasattr(chat_ctx, 'to_dict') and callable(getattr(chat_ctx, 'to_dict')):
                        dict_data = chat_ctx.to_dict()
                        messages = dict_data.get('messages', [])
                    else: messages = []
                except Exception as e:
                    logger.error(f"Error accessing messages from chat_ctx: {e}"); messages = []

                if not messages:
                    logger.warning("Empty chat history or couldn't access messages")
                    yield ChatChunk(id=str(uuid.uuid4()), delta=ChoiceDelta(role='assistant', content="I didn't receive any message to process."))
                    return

                user_messages = []
                for msg in reversed(messages):
                    try:
                        is_user_message = False
                        if hasattr(msg, 'role') and str(getattr(msg, 'role')).lower() == 'user': is_user_message = True
                        elif isinstance(msg, dict) and 'role' in msg and str(msg['role']).lower() == 'user': is_user_message = True
                        if is_user_message: user_messages.append(msg)
                    except Exception as e: logger.error(f"Error checking message type: {e}")
                
                user_message = user_messages[0] if user_messages else None
                transcript = ""
                try:
                    if user_message:
                        content = getattr(user_message, 'content', user_message.get('content') if isinstance(user_message, dict) else str(user_message))
                        if isinstance(content, list): transcript = ' '.join(str(item) for item in content)
                        else: transcript = str(content)
                    logger.info(f"CustomLLMBridge: Extracted transcript: '{transcript}'")
                except Exception as e:
                    logger.error(f"Error extracting content from user message: {e}"); transcript = "[Error: Could not extract transcript]"
                # --- [End of existing logic for transcript extraction] ---

                logger.info(f"Preparing payload for streaming to {self._streaming_agent_url} with transcript: '{transcript}' for page: {self._page_name}")

                # --- [Existing logic for preparing payload - largely unchanged] ---
                payload = {"transcript": transcript, "user_token": self._user_token, "user_id": self._user_id}
                if self._rox_agent_ref:
                    student_context = self._rox_agent_ref._latest_student_context
                    session_id_from_context = self._rox_agent_ref._latest_session_id
                    if student_context:
                        if not isinstance(student_context, dict):
                            try: student_context = json.loads(student_context) if isinstance(student_context, str) else {"user_id": "default_user", "data": str(student_context)}
                            except: student_context = {"user_id": "default_user", "error": "failed_to_parse_context"}
                        if "user_id" not in student_context: student_context["user_id"] = "default_bridge_user"
                        if "task_stage" not in student_context: student_context["task_stage"] = "DEFAULT_CONVERSATION"
                        payload['current_context'] = student_context
                    else:
                        payload['current_context'] = {"user_id": "default_bridge_user", "task_stage": "DEFAULT_CONVERSATION"}
                    
                    if session_id_from_context: payload['session_id'] = session_id_from_context
                    else: payload['session_id'] = f"bridge_session_{uuid.uuid4().hex[:8]}_{int(time.time())}"
                else:
                    payload['current_context'] = {"user_id": "no_agent_ref_user", "task_stage": "DEFAULT_CONVERSATION"}
                    payload['session_id'] = f"no_ref_session_{uuid.uuid4().hex[:8]}"
                payload['_debug_source'] = 'custom_llm_bridge_streaming'
                # --- [End of existing logic for payload preparation] ---
                
                logger.info(f"CustomLLMBridge: Sending payload to {self._streaming_agent_url}: {json.dumps(payload, indent=2)}")

                # Define the TTS pusher function
                # IMPORTANT: You MUST verify this line. It assumes your AgentSession's TTS object
                # has a method like `synthesize_text_chunk` or similar to push raw text for synthesis.
                if self._rox_agent_ref and hasattr(self._rox_agent_ref, 'agent_session') and \
                   hasattr(self._rox_agent_ref.agent_session, 'tts') and \
                   hasattr(self._rox_agent_ref.agent_session.tts, 'synthesize_text_chunk'): # Example method name
                    # This is a conceptual placeholder. Replace with the actual method.
                    # It might be `push_text_chunk`, `_synthesize_chunk`, or something else.
                    # It needs to be an async function or a function that can be awaited.
                    async def tts_pusher(text_chunk: str):
                        logger.debug(f"TTS Pusher: Synthesizing chunk: '{text_chunk}'")
                        await self._rox_agent_ref.agent_session.tts.push_text_chunk(text_chunk)
                    logger.info("TTS pusher function configured to use RoxAgent's TTS synthesis.")
                else:
                    async def tts_pusher(text_chunk: str): # Dummy pusher if no agent_ref or TTS
                        logger.warning(f"TTS Pusher (Dummy): Received text chunk: '{text_chunk}'. RoxAgent or TTS not configured for direct push.")
                    logger.warning("TTS pusher function is a DUMMY. TTS will not be synthesized via streaming push.")

                # Call the streaming function
                ui_actions_from_stream = await self._call_fastapi_streaming_sse(payload, tts_pusher)
                
                tool_calls = []
                if ui_actions_from_stream:
                    # Format UI actions as tool_calls, similar to previous dom_actions logic
                    ui_actions_str = json.dumps(ui_actions_from_stream)
                    tool_call = {
                        "call_id": f"ui_actions_tool_call_{uuid.uuid4().hex[:8]}",
                        "name": "_internal_dom_actions", # Keep for compatibility
                        "arguments": ui_actions_str
                    }
                    tool_calls.append(tool_call)
                    logger.info(f"Created special tool call with UI actions from stream: {ui_actions_from_stream}")

                # Yield a single ChatChunk at the end.
                # Content is empty because TTS was handled by the pusher.
                # Tool_calls will carry any UI actions.
                final_chat_chunk = ChatChunk(
                    id=str(uuid.uuid4()),
                    delta=ChoiceDelta(
                        role='assistant',
                        content="", # TTS is streamed, so main content here is empty
                        tool_calls=tool_calls
                    )
                )
                yield final_chat_chunk
                logger.debug("Finished yielding final ChatChunk from CustomLLMBridge after streaming.")
            
            yield response_generator()
        except Exception as e:
            logger.error(f"Error in _chat_context_manager: {e}", exc_info=True)
            # Yield an error message chunk if something goes wrong in the context manager itself
            # This part might not be directly reachable if response_generator handles its own errors
            # but it's a fallback.
            yield ChatChunk(
                id=str(uuid.uuid4()),
                delta=ChoiceDelta(role='assistant', content="Sorry, an internal error occurred while setting up the response.")
            )
            # Do not re-raise here if you want the agent to try to say something,
            # but for debugging, re-raising might be useful.
            # raise