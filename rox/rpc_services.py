import logging
import json
import base64
import uuid
import time
import asyncio
from livekit.rtc.rpc import RpcInvocationData # For RPC invocation data
from livekit.agents import JobContext # Added for RPC context
import json
from generated.protos import interaction_pb2

logger = logging.getLogger(__name__)

class AgentInteractionService: # Simple class without inheritance
    def __init__(self, agent_instance=None):
        # No base class, so no super().__init__() needed
        self.agent_instance = agent_instance
        logger.info("!!!!!! DEBUG: AgentInteractionService initialized. !!!!!")

    async def HandleFrontendButton(self, invocation_data: RpcInvocationData) -> str:
        print("!!!!!! DEBUG: HandleFrontendButton ENTERED (once) !!!!!!")
        logger.info("!!!!!! DEBUG: HandleFrontendButton ENTERED VIA LOGGER (once) !!!!!!")

        # Deserialize the request from invocation_data.payload
        request = interaction_pb2.FrontendButtonClickRequest()
        # The payload is a base64-encoded string from the LiveKit client
        # We need to decode it first before parsing
        try:
            if isinstance(invocation_data.payload, str):
                logger.info(f"RPC HandleFrontendButton: Payload is a string, assuming base64 encoded. Length: {len(invocation_data.payload)}")
                try:
                    decoded_bytes = base64.b64decode(invocation_data.payload)
                    logger.info(f"Base64 decoded payload. Length: {len(decoded_bytes)}")
                    payload_bytes = decoded_bytes
                except Exception as e:
                    logger.error(f"Failed to decode base64 payload: {e}")
                    payload_bytes = invocation_data.payload.encode('utf-8')
                    logger.info(f"Using raw UTF-8 encoded payload. Length: {len(payload_bytes)}")
            elif isinstance(invocation_data.payload, bytes):
                logger.info(f"RPC HandleFrontendButton: Payload is already bytes. Length: {len(invocation_data.payload)}")
                payload_bytes = invocation_data.payload
            else:
                logger.error(f"RPC HandleFrontendButton: Cannot handle payload type {type(invocation_data.payload)}")
                error_response = interaction_pb2.AgentResponse(status_message=f"Error: Unhandled payload type {type(invocation_data.payload)}", data_payload="")
                serialized_error = error_response.SerializeToString()
                return base64.b64encode(serialized_error).decode('utf-8')
            
            logger.info(f"Attempting to parse protobuf message from payload. First 100 bytes: {payload_bytes[:100]}")
            request.ParseFromString(payload_bytes)
            logger.info(f"Successfully parsed FrontendButtonClickRequest: button_id={request.button_id}, custom_data={request.custom_data}")
        except Exception as e:
            logger.error(f"RPC HandleFrontendButton: Failed to parse payload into FrontendButtonClickRequest: {e}", exc_info=True)
            logger.error(f"Received payload (first 100 bytes): {payload_bytes[:100] if 'payload_bytes' in locals() else 'unknown'}")
            error_response = interaction_pb2.AgentResponse(
                status_message=f"Error processing request: {str(e)}",
                data_payload="Parse error"
            )
            serialized_error = error_response.SerializeToString()
            return base64.b64encode(serialized_error).decode('utf-8')

        logger.info(f"RPC: HandleFrontendButton called by participant: {invocation_data.caller_identity}")
        logger.info(f"RPC: Request button_id='{request.button_id}', custom_data='{request.custom_data}'")

        # Process custom_data if present and agent_instance exists
        if self.agent_instance and request.custom_data:
            try: # Line 65
                # Log the raw custom_data for debugging
                logger.info(f"RPC: Raw custom_data received: {request.custom_data}")
                
                raw_custom_str = request.custom_data
                default_user_id = f"default_{invocation_data.caller_identity or 'user'}"
                parsed_custom_data = {} # Initialize

                try:
                    # Attempt to parse as JSON
                    data = json.loads(raw_custom_str)
                    if isinstance(data, dict):
                        parsed_custom_data = data
                        if "user_id" not in parsed_custom_data:
                            logger.info(f"RPC: Parsed JSON dict is missing 'user_id'. Adding default: {default_user_id}")
                            parsed_custom_data["user_id"] = default_user_id
                    else:
                        # JSON, but not a dict (e.g., a list, string, number)
                        logger.info(f"RPC: custom_data parsed as JSON but is not a dict (type: {type(data)}). Wrapping it.")
                        parsed_custom_data = {
                            "user_id": default_user_id,
                            "value": data  # Store the non-dict JSON data under 'value'
                        }
                except json.JSONDecodeError:
                    # Not valid JSON, treat as a plain string message
                    logger.info(f"RPC: json.loads failed for custom_data. Treating as plain string. Data: '{raw_custom_str}'")
                    parsed_custom_data = {
                        "user_id": default_user_id,
                        "message": raw_custom_str # Store plain string under 'message'
                    }
                except Exception as e:
                    # Catch-all for other unexpected errors during the above processing
                    logger.error(f"RPC: Unexpected error processing custom_data '{raw_custom_str}': {e}", exc_info=True)
                    parsed_custom_data = {
                        "user_id": default_user_id,
                        "error": "custom_data_processing_error",
                        "original_custom_data": raw_custom_str,
                        "exception": str(e)
                    }
                
                # Ensure parsed_custom_data is always a dict and has a user_id if not an error structure
                if not isinstance(parsed_custom_data, dict):
                     logger.error("RPC: parsed_custom_data is not a dict after processing. This is unexpected. Forcing error structure.")
                     parsed_custom_data = {
                        "user_id": default_user_id,
                        "error": "internal_custom_data_handling_failed",
                        "original_custom_data": raw_custom_str
                     }
                elif "user_id" not in parsed_custom_data and "error" not in parsed_custom_data : # Check if it's a dict but missing user_id and not an error dict
                    logger.warning(f"RPC: Processed custom_data is a dict but missing 'user_id'. Adding default: {default_user_id}")
                    parsed_custom_data["user_id"] = default_user_id

                # Store the validated context
                if self.agent_instance:
                    self.agent_instance._latest_student_context = parsed_custom_data
                    logger.info(f"RPC: Updated agent_instance._latest_student_context with: {parsed_custom_data}")

                    # Handle session ID (prioritize session_id over sessionId)
                    if isinstance(parsed_custom_data, dict) and 'session_id' in parsed_custom_data:
                        self.agent_instance._latest_session_id = parsed_custom_data['session_id']
                        logger.info(f"RPC: Updated agent_instance._latest_session_id with: {parsed_custom_data['session_id']}")
                    elif isinstance(parsed_custom_data, dict) and 'sessionId' in parsed_custom_data: # Fallback for camelCase
                        self.agent_instance._latest_session_id = parsed_custom_data['sessionId']
                        logger.info(f"RPC: Updated agent_instance._latest_session_id with (from sessionId): {parsed_custom_data['sessionId']}")
                    else:
                        # Generate a session ID if none exists or not found in custom_data
                        if not hasattr(self.agent_instance, '_latest_session_id') or not self.agent_instance._latest_session_id:
                            new_session_id = f"session_{uuid.uuid4().hex}" # Use .hex for a clean string
                            self.agent_instance._latest_session_id = new_session_id
                            if isinstance(parsed_custom_data, dict) and "error" not in parsed_custom_data:
                                parsed_custom_data['session_id'] = new_session_id
                                logger.info(f"RPC: Generated new session_id: {new_session_id}. Agent session ID updated and added to parsed_custom_data.")
                            else: 
                                logger.info(f"RPC: Generated new session_id: {new_session_id}. Agent session ID updated. Not adding to parsed_custom_data as it's not a valid dict or is an error structure.")
                else:
                    logger.warning("RPC: agent_instance is None, cannot update context or session ID.")
                
                # Prepare UI action data for client-side RPC
                action_data_for_client_rpc = {
                    "action_type_str": "SHOW_ALERT", 
                    "parameters": { 
                        "title": "Button Clicked",
                        "message": f"Button '{request.button_id}' was clicked by {invocation_data.caller_identity}. Context updated.",
                        "buttons": [
                            {
                                "label": "OK",
                                "action": {"action_type": interaction_pb2.UIAction.ActionType.DISMISS_ALERT} 
                            }
                        ]
                    }
                }
                logger.info(f"RPC: Preparing UI action (alert) data for client-side RPC: {action_data_for_client_rpc['parameters']}")
                logger.info(f"RPC DEBUG: Type of self.agent_instance: {type(self.agent_instance)}")
                
                try:
                    alert_params = action_data_for_client_rpc.get('parameters', {})
                    alert_buttons_data = alert_params.get('buttons', [])
                    
                    alert_parameters_for_rpc = {}
                    raw_alert_params = action_data_for_client_rpc.get('parameters', {})

                    for key, value in raw_alert_params.items():
                        if isinstance(value, (dict, list)):
                            alert_parameters_for_rpc[key] = json.dumps(value)
                        else:
                            alert_parameters_for_rpc[key] = str(value)
                    
                    logger.info(f"RPC: Serialized alert parameters for AgentToClientUIActionRequest: {alert_parameters_for_rpc}")

                    payload = interaction_pb2.AgentToClientUIActionRequest(
                        request_id=str(uuid.uuid4()),
                        action_type=interaction_pb2.ClientUIActionType.SHOW_ALERT, # This is from action_data_for_client_rpc
                        parameters=alert_parameters_for_rpc
                    )
                    payload_bytes = payload.SerializeToString()

                    # This is where we might trigger a backend call or another UI action
                    # For now, let's just log and maybe echo a response.
                    # Example of sending a UI action back to the frontend:
                    target_client_identity = invocation_data.caller_identity

                    # CONSTRUCT THE DICTIONARY for the send_ui_action_to_frontend method
                    action_data_for_agent = {
                        "action_type_str": "SHOW_ALERT", # Example action, maps to ClientUIActionType enum
                        "request_id": str(uuid.uuid4()),
                        "parameters": {
                            "title": "Backend Test", 
                            "message": f"Button '{request.button_id}' clicked."
                        }
                    }

                    # The actual call that is causing the error - NOW FIXED
                    await self.agent_instance.send_ui_action_to_frontend(
                        action_data=action_data_for_agent, 
                        target_identity=target_client_identity,
                        job_ctx_override=self.agent_instance._job_ctx # Use JobContext from RpcInvocationData
                    )
                    logger.info(f"RPC: Successfully called send_ui_action_to_frontend for {target_client_identity}")
                except Exception as e:
                    logger.error(f"Error in HandleFrontendButton while sending UI action: {e}")
            except Exception as main_e: 
                logger.error(f"RPC: Error processing custom_data or sending alert: {main_e}", exc_info=True)
        elif not self.agent_instance:
            logger.warning("RPC: agent_instance is not available. Cannot update context.")
        elif not request.custom_data:
            logger.info("RPC: No custom_data received in this request.")
        
        # Send a test request to the backend to verify context passing
        if self.agent_instance:
            try:
                import aiohttp
                
                # Create a task to send the request asynchronously
                loop = asyncio.get_event_loop()
                task = asyncio.create_task(self._send_test_request_to_backend(job_ctx=self.agent_instance._job_ctx, invocation_data=invocation_data)) # Pass JobContext
                
                # Log that we're sending a test request
                logger.info("RPC: Created async task to send test request to backend API")
            except Exception as e:
                logger.error(f"RPC: Failed to create test request task: {e}")
        
        response_message = f"Button '{request.button_id}' click processed by RoxAgent."
        if request.custom_data:
            response_message += f" Data: '{request.custom_data}'"

        response = interaction_pb2.AgentResponse(
            status_message=response_message,
            data_payload="Successfully processed by agent."
        )
        
        logger.info(f"Sending response: {response}")
        serialized_response = response.SerializeToString()
        logger.info(f"Serialized response length: {len(serialized_response)} bytes")
        
        base64_response = base64.b64encode(serialized_response).decode('utf-8')
        logger.info(f"Base64 encoded response length: {len(base64_response)} characters")
        
        return base64_response
        
    async def _send_test_request_to_backend(self, job_ctx: JobContext, invocation_data: RpcInvocationData):
        """Send a request to the backend API and handle the streaming response."""
        import aiohttp
        import os
        import json # Ensure json is imported
        import uuid # For request_id in UI actions

        if not self.agent_instance:
            logger.error("Cannot send request: agent_instance is not available")
            return

        base_backend_url = os.environ.get("MY_CUSTOM_AGENT_URL", "http://localhost:8001/process_interaction")
        # Ensure we are targeting the streaming endpoint
        if "/process_interaction_streaming" not in base_backend_url:
            streaming_backend_url = base_backend_url.replace("/process_interaction", "/process_interaction_streaming")
        else:
            streaming_backend_url = base_backend_url

        try:
            logger.info(f"Sending request to backend streaming API at {streaming_backend_url}")

            # Ensure _latest_student_context is a dictionary
            raw_context = self.agent_instance._latest_student_context
            if not isinstance(raw_context, dict):
                logger.warning(f"_latest_student_context is not a dict (type: {type(raw_context)}). Initializing to empty dict for this request.")
                current_interaction_context_data = {}
            else:
                current_interaction_context_data = raw_context.copy() # Work with a copy

            session_id = self.agent_instance._latest_session_id
            
            current_transcript = current_interaction_context_data.get("transcript_from_frontend", current_interaction_context_data.get("message", "No message provided"))

            if 'task_stage' not in current_interaction_context_data:
                current_interaction_context_data['task_stage'] = 'unknown_via_rpc_stream_request'
                logger.info(f"'task_stage' not found in context, adding default: {current_interaction_context_data['task_stage']}")

            payload = {
                "usertoken": current_interaction_context_data.get("usertoken"),
                "session_id": session_id,
                "transcript": current_transcript,
                "current_context": current_interaction_context_data,
                "chat_history": current_interaction_context_data.get("chat_history", [])
            }

            logger.debug(f"Request payload for streaming: {json.dumps(payload, indent=2)}")

            async with aiohttp.ClientSession() as http_session:
                async with http_session.post(streaming_backend_url, json=payload) as response:
                    logger.info(f"Backend API response status: {response.status}")
                    if response.status == 200 and 'text/event-stream' in response.headers.get('Content-Type', ''):
                        logger.info("Backend response is a stream. Parsing SSE...")
                        current_event_name = None
                        current_event_data_lines = []

                        async for line_bytes in response.content:
                            line = line_bytes.decode('utf-8').strip()

                            if line.startswith('event:'):
                                current_event_name = line[len('event:'):].strip()
                            elif line.startswith('data:'):
                                current_event_data_lines.append(line[len('data:'):].strip())
                            elif not line:
                                if current_event_name and current_event_data_lines:
                                    data_str = "\n".join(current_event_data_lines)
                                    logger.debug(f"SSE: Received event '{current_event_name}' with data: {data_str[:200]}...")
                                    
                                    try:
                                        data_json = json.loads(data_str)

                                        if current_event_name == "streaming_text_chunk":
                                            text_to_speak = data_json.get('streaming_text_chunk')
                                            if text_to_speak and self.agent_instance and hasattr(self.agent_instance, 'speak_text'):
                                                logger.info(f"SSE: Speaking text from 'streaming_text_chunk': {text_to_speak[:100]}...")
                                                await self.agent_instance.speak_text(text_to_speak)
                                            else:
                                                logger.warning("SSE: 'streaming_text_chunk' received but no text found or agent can't speak.")

                                        elif current_event_name == "final_ui_actions":
                                            ui_actions = data_json.get('ui_actions', [])
                                            if ui_actions:
                                                logger.info(f"SSE: Received 'final_ui_actions' with {len(ui_actions)} actions.")
                                                logger.debug(f"UI Actions to be processed: {ui_actions}")
                                            else:
                                                logger.warning("SSE: Received 'final_ui_actions' but the 'ui_actions' key was empty or missing.")

                                        elif current_event_name == "stream_end":
                                            logger.info(f"SSE: Received stream_end event. Message: {data_json.get('message', 'Stream ended')}")
                                        
                                        else:
                                            logger.warning(f"SSE: Received unhandled event type: {current_event_name}")

                                    except json.JSONDecodeError:
                                        logger.error(f"SSE: JSONDecodeError for event '{current_event_name}' with data: {data_str}")
                                    except Exception as e:
                                        logger.error(f"SSE: Error processing event '{current_event_name}': {e}", exc_info=True)

                                    current_event_name = None
                                    current_event_data_lines = []
                        
                        logger.info("Finished consuming backend SSE stream.")
                    else:
                        response_text = await response.text()
                        logger.error(f"Backend API returned non-streaming or error response: Status={response.status}, Body={response_text}")

        except aiohttp.ClientConnectorError as e:
            logger.error(f"Connection error sending request to backend: {e}")
        except Exception as e:
            logger.error(f"An unexpected error occurred in _send_test_request_to_backend: {e}", exc_info=True)
    async def NotifyPageLoad(self, invocation_data: RpcInvocationData) -> str:
        logger.info(f"RPC NotifyPageLoad: Received call from participant: {invocation_data.caller_identity}")

        request = interaction_pb2.NotifyPageLoadRequest()
        try:
            if isinstance(invocation_data.payload, str):
                logger.info(f"RPC NotifyPageLoad: Payload is a string, assuming base64 encoded. Length: {len(invocation_data.payload)}")
                try:
                    decoded_bytes = base64.b64decode(invocation_data.payload)
                    payload_bytes = decoded_bytes
                except Exception as e:
                    logger.error(f"RPC NotifyPageLoad: Failed to decode base64 payload: {e}")
                    payload_bytes = invocation_data.payload.encode('utf-8') # Fallback
            elif isinstance(invocation_data.payload, bytes):
                logger.info(f"RPC NotifyPageLoad: Payload is already bytes. Length: {len(invocation_data.payload)}")
                payload_bytes = invocation_data.payload
            else:
                logger.error(f"RPC NotifyPageLoad: Cannot handle payload type {type(invocation_data.payload)}")
                error_response = interaction_pb2.AgentResponse(status_message=f"Error: Unhandled payload type {type(invocation_data.payload)}")
                serialized_error = error_response.SerializeToString()
                return base64.b64encode(serialized_error).decode('utf-8')

            request.ParseFromString(payload_bytes)
            logger.info(f"RPC NotifyPageLoad: Successfully parsed NotifyPageLoadRequest: user_id='{request.user_id}', page='{request.current_page}', session_id='{request.session_id}'")

        except Exception as e:
            logger.error(f"RPC NotifyPageLoad: Failed to parse payload: {e}", exc_info=True)
            error_response = interaction_pb2.AgentResponse(
                status_message=f"Error processing NotifyPageLoad request: {str(e)}",
                data_payload="Parse error"
            )
            serialized_error = error_response.SerializeToString()
            return base64.b64encode(serialized_error).decode('utf-8')

        if self.agent_instance:
            # Update agent's context
            page_load_context = {
                "user_id": request.user_id,
                "task_stage": request.task_stage,
                "current_page": request.current_page,
                "session_id": request.session_id,
                "chat_history": request.chat_history, # Assuming it's a JSON string as sent by client
                "transcript": request.transcript, # Optional field
                "page_load_timestamp": time.time()
            }
            self.agent_instance._latest_student_context = page_load_context
            self.agent_instance._latest_session_id = request.session_id
            logger.info(f"RPC NotifyPageLoad: Agent context updated. User: '{request.user_id}', Page: '{request.current_page}', Session: '{request.session_id}'")
            logger.debug(f"RPC NotifyPageLoad: Full context updated: {page_load_context}")

            # --- START MODIFICATION FOR TIMER TEST ---
            if request.current_page == 'writingpractisetest':
                logger.info(f"RPC NotifyPageLoad: Page is 'writingpractisetest'. Attempting to send START_TIMER command to {invocation_data.caller_identity}.")
                try:
                    timer_duration = 10  # seconds
                    ui_action_request = interaction_pb2.AgentToClientUIActionRequest(
                        request_id=str(uuid.uuid4()),
                        action_type=interaction_pb2.ClientUIActionType.START_TIMER,
                        parameters={"duration_seconds": str(timer_duration)}
                    )
                    
                    if hasattr(self.agent_instance, 'send_action_to_participant'):
                        await self.agent_instance.send_action_to_participant(
                            participant_identity=invocation_data.caller_identity,
                            action_request=ui_action_request
                        )
                        logger.info(f"RPC NotifyPageLoad: START_TIMER command ({timer_duration}s) sent to {invocation_data.caller_identity}.")
                    elif hasattr(self.agent_instance, '_room_manager') and hasattr(self.agent_instance._room_manager, 'send_action_to_participant_in_default_room'):
                         await self.agent_instance._room_manager.send_action_to_participant_in_default_room(
                            participant_identity=invocation_data.caller_identity,
                            action_request=ui_action_request
                        )
                         logger.info(f"RPC NotifyPageLoad: START_TIMER command ({timer_duration}s) sent via RoomManager to {invocation_data.caller_identity}.")
                    else:
                        logger.error("RPC NotifyPageLoad: Agent instance does not have a recognized method to send action to participant. Timer not started.")

                except Exception as e:
                    logger.error(f"RPC NotifyPageLoad: Failed to send START_TIMER command: {e}", exc_info=True)
            # --- END MODIFICATION FOR TIMER TEST ---

            status_msg = f"Page load notification for '{request.current_page}' by user '{request.user_id}' received and processed."
            data_payload_str = f"Session ID: {request.session_id}, Context Updated."
        else:
            logger.warning("RPC NotifyPageLoad: agent_instance is None. Cannot update context.")
            status_msg = "Page load notification received, but agent instance not available to update context."
            data_payload_str = "Agent instance not found."

        response = interaction_pb2.AgentResponse(
            status_message=status_msg,
            data_payload=data_payload_str
        )
        serialized_response = response.SerializeToString()
        return base64.b64encode(serialized_response).decode('utf-8')