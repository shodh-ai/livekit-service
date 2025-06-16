import logging
import json
import base64
import uuid
import time
import asyncio

from livekit.agents import JobContext # Added for RPC context, simplified import
from livekit.rtc.rpc import RpcInvocationData # Added import
import json
from generated.protos import interaction_pb2

logger = logging.getLogger(__name__)

class AgentInteractionService: # Simple class without inheritance
    def __init__(self, agent_instance=None):
        # No base class, so no super().__init__() needed
        self.agent_instance = agent_instance
        logger.info("!!!!!! DEBUG: AgentInteractionService initialized. !!!!!")

    async def HandleFrontendButton(self, raw_payload: RpcInvocationData) -> str:
        logger.info("[RPC DEBUG] HandleFrontendButton method invoked by frontend (via RpcInvocationData).")
        try:
            base64_decoded_payload = base64.b64decode(raw_payload.payload)
            request = interaction_pb2.FrontendButtonClickRequest()
            request.ParseFromString(base64_decoded_payload)
            logger.info(f"RPC HandleFrontendButton: Decoded request: button_id='{request.button_id}', custom_data='{request.custom_data}'")
        except Exception as e_decode:
            logger.error(f"RPC HandleFrontendButton: Error decoding RpcInvocationData payload: {e_decode}", exc_info=True)
            error_response_pb = interaction_pb2.AgentResponse(status_message=f"Error decoding request payload: {e_decode}")
            return base64.b64encode(error_response_pb.SerializeToString()).decode('utf-8')

        try:
            # Ensure agent_instance and _job_ctx are available
            if not self.agent_instance or \
               not hasattr(self.agent_instance, '_job_ctx') or \
               not self.agent_instance._job_ctx or \
               not self.agent_instance._job_ctx.room or \
               not self.agent_instance._job_ctx.room.local_participant:
                logger.error("RPC HandleFrontendButton: agent_instance, _job_ctx, room, or local_participant not available. Cannot proceed.")
                error_response_pb = interaction_pb2.AgentResponse(status_message="Internal server error: agent context not ready.")
                return base64.b64encode(error_response_pb.SerializeToString()).decode('utf-8')

            caller_identity = self.agent_instance._job_ctx.room.local_participant.identity
            logger.info(f"RPC HandleFrontendButton: Caller identity from JobContext: {caller_identity}")

            if request.button_id == "send_test_request_to_backend_api":
                logger.info("RPC: 'send_test_request_to_backend_api' button clicked. Creating task for _send_test_request_to_backend.")
                asyncio.create_task(self._send_test_request_to_backend(self.agent_instance._job_ctx, request, caller_identity))
                logger.info("RPC: Created async task to send test request to backend API")
            
            elif request.button_id == "test_rpc_button":
                logger.info("RPC: 'test_rpc_button' clicked. Processing custom_data and initiating LangGraph request.")
                # Process custom_data (copied from below, simplified for this block)
                if request.custom_data:
                    logger.info(f"RPC (test_rpc_button): Raw custom_data received: {request.custom_data}")
                    raw_custom_str = request.custom_data
                    default_user_id = f"default_{caller_identity or 'user'}"
                    parsed_custom_data = {}
                    try:
                        data = json.loads(raw_custom_str)
                        if isinstance(data, dict):
                            parsed_custom_data = data
                            if "user_id" not in parsed_custom_data: parsed_custom_data["user_id"] = default_user_id
                        else:
                            parsed_custom_data = {"user_id": default_user_id, "value": data}
                    except json.JSONDecodeError:
                        parsed_custom_data = {"user_id": default_user_id, "message": raw_custom_str}
                    except Exception as e_custom_data_parse: # Simplified error handling for brevity
                        logger.error(f"RPC (test_rpc_button): Error processing custom_data: {e_custom_data_parse}")
                        parsed_custom_data = {"user_id": default_user_id, "error": "custom_data_processing_error"}

                    self.agent_instance._latest_student_context = parsed_custom_data
                    logger.info(f"RPC (test_rpc_button): Updated agent_instance._latest_student_context with: {parsed_custom_data}")
                    # Handle session ID (copied and simplified)
                    if isinstance(parsed_custom_data, dict) and 'session_id' in parsed_custom_data:
                        self.agent_instance._latest_session_id = parsed_custom_data['session_id']
                    elif isinstance(parsed_custom_data, dict) and 'sessionId' in parsed_custom_data: # Check for sessionId as well
                        self.agent_instance._latest_session_id = parsed_custom_data['sessionId']
                    elif not hasattr(self.agent_instance, '_latest_session_id') or not self.agent_instance._latest_session_id:
                        new_session_id = f"session_{uuid.uuid4().hex}"
                        self.agent_instance._latest_session_id = new_session_id
                        if isinstance(parsed_custom_data, dict) and "error" not in parsed_custom_data:
                             parsed_custom_data['session_id'] = new_session_id # Add to context if created
                    logger.info(f"RPC (test_rpc_button): Agent session ID is now: {getattr(self.agent_instance, '_latest_session_id', 'not set')}")

                # Initiate LangGraph request
                logger.info("RPC (test_rpc_button): Creating task to send request to LangGraph service.")
                asyncio.create_task(self._send_test_request_to_backend(self.agent_instance._job_ctx, request, caller_identity))
                logger.info("RPC (test_rpc_button): Created async task to send request to LangGraph service.")

                # Send specific UI alert for this button
                try:
                    action_data_for_agent = {
                        "action_type_str": "SHOW_ALERT",
                        "request_id": str(uuid.uuid4()),
                        "parameters": {
                            "title": "LangGraph Request",
                            "message": f"Button '{request.button_id}' clicked. Sending request to LangGraph...",
                            "buttons": [{"label": "OK", "action": {"action_type": interaction_pb2.UIAction.ActionType.DISMISS_ALERT}}]
                        }
                    }
                    await self.agent_instance.send_ui_action_to_frontend(
                        action_data=action_data_for_agent,
                        target_identity=caller_identity,
                        job_ctx_override=self.agent_instance._job_ctx
                    )
                    logger.info(f"RPC (test_rpc_button): Successfully sent UI alert for LangGraph initiation to {caller_identity}")
                except Exception as e_ui_action:
                    logger.error(f"RPC (test_rpc_button): Error sending UI alert: {e_ui_action}", exc_info=True)
            
            # Fallback for other buttons with custom_data that are not 'send_test_request_to_backend_api' or 'test_rpc_button'
            elif request.custom_data: 
                logger.info(f"RPC: Processing custom_data for button_id '{request.button_id}'.")
                # Full custom_data processing logic (lines 53-94 from original)
                raw_custom_str = request.custom_data
                default_user_id = f"default_{caller_identity or 'user'}"
                parsed_custom_data = {} 
                try:
                    data = json.loads(raw_custom_str)
                    if isinstance(data, dict):
                        parsed_custom_data = data
                        if "user_id" not in parsed_custom_data:
                            logger.info(f"RPC: Parsed JSON dict is missing 'user_id'. Adding default: {default_user_id}")
                            parsed_custom_data["user_id"] = default_user_id
                    else:
                        logger.info(f"RPC: custom_data parsed as JSON but is not a dict (type: {type(data)}). Wrapping it.")
                        parsed_custom_data = {"user_id": default_user_id, "value": data}
                except json.JSONDecodeError:
                    logger.info(f"RPC: json.loads failed for custom_data. Treating as plain string. Data: '{raw_custom_str}'")
                    parsed_custom_data = {"user_id": default_user_id, "message": raw_custom_str}
                except Exception as e_custom_data_parse:
                    logger.error(f"RPC: Unexpected error processing custom_data '{raw_custom_str}': {e_custom_data_parse}", exc_info=True)
                    parsed_custom_data = {"user_id": default_user_id, "error": "custom_data_processing_error", "original_custom_data": raw_custom_str, "exception": str(e_custom_data_parse)}
                
                if not isinstance(parsed_custom_data, dict):
                     parsed_custom_data = {"user_id": default_user_id, "error": "internal_custom_data_handling_failed", "original_custom_data": raw_custom_str}
                elif "user_id" not in parsed_custom_data and "error" not in parsed_custom_data:
                    parsed_custom_data["user_id"] = default_user_id

                self.agent_instance._latest_student_context = parsed_custom_data
                logger.info(f"RPC: Updated agent_instance._latest_student_context with: {parsed_custom_data}")
                
                if isinstance(parsed_custom_data, dict) and 'session_id' in parsed_custom_data:
                    self.agent_instance._latest_session_id = parsed_custom_data['session_id']
                elif isinstance(parsed_custom_data, dict) and 'sessionId' in parsed_custom_data:
                    self.agent_instance._latest_session_id = parsed_custom_data['sessionId']
                elif not hasattr(self.agent_instance, '_latest_session_id') or not self.agent_instance._latest_session_id:
                    new_session_id = f"session_{uuid.uuid4().hex}"
                    self.agent_instance._latest_session_id = new_session_id
                    if isinstance(parsed_custom_data, dict) and "error" not in parsed_custom_data:
                        parsed_custom_data['session_id'] = new_session_id
                logger.info(f"RPC: Agent session ID is now: {getattr(self.agent_instance, '_latest_session_id', 'not set')}")

                # Generic UI action for other buttons with custom_data
                try:
                    action_data_for_client_rpc = {
                        "action_type_str": "SHOW_ALERT", 
                        "parameters": { 
                            "title": "Button Clicked",
                            "message": f"Button '{request.button_id}' was clicked by {caller_identity}. Context updated.",
                            "buttons": [{"label": "OK", "action": {"action_type": interaction_pb2.UIAction.ActionType.DISMISS_ALERT}}]
                        }
                    }
                    logger.info(f"RPC: Preparing UI action (alert) data for client-side RPC: {action_data_for_client_rpc['parameters']}")
                    
                    action_data_for_agent = {
                        "action_type_str": "SHOW_ALERT",
                        "request_id": str(uuid.uuid4()),
                        "parameters": {"title": "Backend Test", "message": f"Button '{request.button_id}' clicked."}
                    }
                    await self.agent_instance.send_ui_action_to_frontend(
                        action_data=action_data_for_agent, 
                        target_identity=caller_identity,
                        job_ctx_override=self.agent_instance._job_ctx
                    )
                    logger.info(f"RPC: Successfully called send_ui_action_to_frontend for {caller_identity}")
                except Exception as e_ui_action:
                    logger.error(f"Error in HandleFrontendButton while sending UI action: {e_ui_action}", exc_info=True)
            else: # No custom_data
                logger.info("RPC: No custom_data received in this request.")

            # Final response after all processing
            response_message = f"Button '{request.button_id}' click processed by RoxAgent."
            if request.custom_data:
                response_message += f" Data processed."
            
            response_pb = interaction_pb2.AgentResponse(
                status_message=response_message,
                data_payload="Successfully processed by agent."
            )
            serialized_response = response_pb.SerializeToString()
            return base64.b64encode(serialized_response).decode('utf-8')

        except Exception as main_e: 
            logger.error(f"RPC HandleFrontendButton: General error processing request: {main_e}", exc_info=True)
            error_response_pb = interaction_pb2.AgentResponse(status_message=f"Error processing request: {main_e}")
            return base64.b64encode(error_response_pb.SerializeToString()).decode('utf-8')
        
    async def _send_test_request_to_backend(self, job_ctx: JobContext, parsed_request: interaction_pb2.FrontendButtonClickRequest, caller_identity: str) -> None:
        """Send a request to the backend API and handle the streaming response."""
        logger.info("!!!!!! DEBUG: _send_test_request_to_backend ENTERED (once) !!!!!!")
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
            # Use the already parsed_request and caller_identity
            button_id_for_backend = parsed_request.button_id
            custom_data_for_backend = parsed_request.custom_data
            logger.info(f"_send_test_request_to_backend: Processing button_id: {button_id_for_backend}, custom_data: {custom_data_for_backend} for caller: {caller_identity}")

    async def NotifyPageLoad(self, raw_payload: RpcInvocationData) -> str:
        logger.info("[RPC DEBUG] NotifyPageLoad method invoked by frontend (via RpcInvocationData).")
        try:
            base64_decoded_payload = base64.b64decode(raw_payload.payload)
            request = interaction_pb2.NotifyPageLoadRequest()
            request.ParseFromString(base64_decoded_payload)
            logger.info(f"RPC NotifyPageLoad: Decoded request: user_id='{request.user_id}', page='{request.current_page}', session_id='{request.session_id}'")
        except Exception as e_decode:
            logger.error(f"RPC NotifyPageLoad: Error decoding RpcInvocationData payload: {e_decode}", exc_info=True)
            error_response_pb = interaction_pb2.AgentResponse(status_message=f"Error decoding request payload: {e_decode}")
            return base64.b64encode(error_response_pb.SerializeToString()).decode('utf-8')

        caller_identity = "unknown_caller" # Default
        try:
            if not self.agent_instance or not hasattr(self.agent_instance, '_job_ctx') or not self.agent_instance._job_ctx or not self.agent_instance._job_ctx.participant:
                logger.warning("RPC NotifyPageLoad: agent_instance, _job_ctx, or participant not available. Cannot retrieve caller identity.")
            else:
                caller_identity = self.agent_instance._job_ctx.participant.identity
                logger.info(f"RPC NotifyPageLoad: Caller identity from JobContext: {caller_identity}")

            # Update agent context with page load information
            if self.agent_instance:
                self.agent_instance.update_agent_context(
                    user_id=request.user_id,
                    session_id=request.session_id,
                    current_page=request.current_page,
                    interaction_type="page_load"
                )
                logger.info(f"Agent context updated for user '{request.user_id}' on page '{request.current_page}'.")

                # Send a UI action to the frontend (e.g., start a timer)
                # The actual sending of UI action should be handled by the agent instance, 
                # possibly by calling a method on self.agent_instance that knows how to dispatch it correctly.
                # For example: await self.agent_instance.dispatch_ui_action_to_participant(caller_identity, "START_TIMER", {"duration": 300, "message": "Timer started by NotifyPageLoad"})
                logger.info(f"Attempting to trigger START_TIMER UI action for participant '{caller_identity}'.")
                # This is a conceptual call. The RoxAgent needs a method to handle this.
                # For now, we assume such a mechanism exists or will be added to RoxAgent.
                # Example: await self.agent_instance.send_ui_action_to_participant(caller_identity, interaction_pb2.UserInterfaceAction(action_id="START_TIMER", details_json=json.dumps({"duration": 300}))) 
                # This part is simplified as the actual dispatch logic is within RoxAgent.

            response_pb = interaction_pb2.AgentResponse(
                status_message="Page load notified and context updated.",
                data_payload=json.dumps({"page": request.current_page, "user_id": request.user_id})
            )
            serialized_response = response_pb.SerializeToString()
            return base64.b64encode(serialized_response).decode('utf-8')

        except Exception as e:
            logger.error(f"RPC NotifyPageLoad: Error processing request: {e}", exc_info=True)
            error_response_pb = interaction_pb2.AgentResponse(status_message=f"Error processing NotifyPageLoad: {e}")
            return base64.b64encode(error_response_pb.SerializeToString()).decode('utf-8')

    async def NotifyPageLoadV2(self, raw_payload: RpcInvocationData) -> str: # Returns str
        logger.info("[RPC V2 DEBUG] NotifyPageLoadV2 method invoked. Attempting LangGraph integration.")
        try:
            caller_identity = raw_payload.caller_identity # This comes from RpcInvocationData directly
            
            # 1. Decode and Parse Protobuf Payload
            if not raw_payload.payload:
                logger.error("RPC NotifyPageLoadV2: Raw payload is empty.")
                raise ValueError("Payload is empty")

            base64_decoded_payload = base64.b64decode(raw_payload.payload)
            # Assuming NotifyPageLoadRequestV2 is defined in your interaction_pb2
            # If it's a different proto, adjust interaction_pb2.NotifyPageLoadRequestV2 accordingly.
            # For now, let's assume it's the same as NotifyPageLoadRequest for structure,
            # or a new NotifyPageLoadRequestV2 if you've defined one.
            # Let's use a generic name `parsed_page_load_request`
            parsed_page_load_request = interaction_pb2.NotifyPageLoadRequest() # Or NotifyPageLoadRequestV2
            parsed_page_load_request.ParseFromString(base64_decoded_payload)

            logger.info(f"RPC NotifyPageLoadV2: Decoded request from {caller_identity}: user_id='{parsed_page_load_request.user_id}', page='{parsed_page_load_request.current_page}', session_id='{parsed_page_load_request.session_id}', task_stage='{parsed_page_load_request.task_stage}'")

            # 2. Populate agent context
            if not self.agent_instance:
                logger.error("RPC NotifyPageLoadV2: agent_instance is not available. Cannot set context or call LangGraph.")
                raise RuntimeError("Agent instance not available")

            student_context_message = f"User '{parsed_page_load_request.user_id}' loaded page '{parsed_page_load_request.current_page}' during task stage '{parsed_page_load_request.task_stage}'."
            
            student_context = {
                "user_id": parsed_page_load_request.user_id,
                "session_id": parsed_page_load_request.session_id,
                "current_page": parsed_page_load_request.current_page,
                "task_stage": parsed_page_load_request.task_stage,
                "message": student_context_message, # This will be used as 'transcript' by _send_test_request_to_backend
                "room_state": parsed_page_load_request.room_state, # Assuming room_state is part of NotifyPageLoadRequest
                "event_type": "page_load" 
            }
            self.agent_instance._latest_student_context = student_context
            self.agent_instance._latest_session_id = parsed_page_load_request.session_id
            logger.info(f"RPC NotifyPageLoadV2: Updated agent context: {student_context}")
            logger.info(f"RPC NotifyPageLoadV2: Agent session ID is now: {self.agent_instance._latest_session_id}")

            # 3. Call LangGraph service
            # _send_test_request_to_backend expects (self, job_ctx, parsed_request_proto, caller_identity)
            # We don't have a job_ctx directly here, but it might be accessible via self.agent_instance._job_ctx
            # The parsed_request_proto is FrontendButtonClickRequest for it. We have NotifyPageLoadRequest.
            # However, it primarily uses self.agent_instance._latest_student_context.
            job_ctx_for_langgraph = getattr(self.agent_instance, '_job_ctx', None)
            if not job_ctx_for_langgraph:
                logger.warning("RPC NotifyPageLoadV2: _job_ctx not found on agent_instance. LangGraph call might have limited context.")
            
            logger.info("RPC NotifyPageLoadV2: Creating task to send request to LangGraph service.")
            # Passing None for parsed_request as _send_test_request_to_backend should use the context we just set.
            asyncio.create_task(self._send_test_request_to_backend(job_ctx_for_langgraph, None, caller_identity))
            logger.info("RPC NotifyPageLoadV2: Created async task to send request to LangGraph service.")

            # 4. Return response to frontend
            response_data = {
                "status": "success",
                "message": f"NotifyPageLoadV2 processed for {caller_identity}. LangGraph request initiated for page '{parsed_page_load_request.current_page}'.",
                "details": {
                    "user_id": parsed_page_load_request.user_id,
                    "session_id": parsed_page_load_request.session_id,
                    "page": parsed_page_load_request.current_page
                }
            }
            json_response_str = json.dumps(response_data)
            base64_response_str = base64.b64encode(json_response_str.encode('utf-8')).decode('utf-8')
            
            logger.info("RPC NotifyPageLoadV2: Sending base64 JSON string response indicating LangGraph initiation.")
            return base64_response_str

        except Exception as e:
            logger.error(f"RPC NotifyPageLoadV2: Error: {e}", exc_info=True)
            # Ensure a consistent error response format
            error_response_data = {
                "status": "error", 
                "message": f"Error in NotifyPageLoadV2: {str(e)}",
                "details": {} # Add more error details if available
            }
            json_error_str = json.dumps(error_response_data)
            base64_error_str = base64.b64encode(json_error_str.encode('utf-8')).decode('utf-8')
            return base64_error_str

    async def TestPing(self, raw_payload: RpcInvocationData) -> str: # Returns str
        logger.info(f"[RPC DEBUG] TestPing method invoked by {raw_payload.caller_identity} (expecting Empty payload).")
        try:
            # The payload for TestPing is google.protobuf.Empty, no need to parse its content.
            # We can optionally decode and verify it's an Empty message if strictness is needed,
            # but for a ping, just acknowledging the call is usually enough.
            logger.info(f"RPC TestPing: Received ping from: {raw_payload.caller_identity}. Payload type should be Empty.")

            response_pb = interaction_pb2.AgentResponse(
                status_message="Pong! TestPing successful."
            )
            # Return as base64 encoded string, matching google.protobuf.StringValue in proto
            return base64.b64encode(response_pb.SerializeToString()).decode('utf-8')

        except Exception as e:
            logger.error(f"RPC TestPing: Error: {e}", exc_info=True)
            error_response_pb = interaction_pb2.AgentResponse(
                status_message=f"Error in TestPing: {str(e)}"
            )
            return base64.b64encode(error_response_pb.SerializeToString()).decode('utf-8')