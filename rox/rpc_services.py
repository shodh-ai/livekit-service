# File: livekit-service/rox/rpc_services.py
# rox/rpc_services.py
"""RPC service handlers for the Rox Conductor.

This module provides specialized RPC methods that form the Conductor's "ears"
for receiving specific, meaningful events from the Frontend Sensor.
"""

import logging
import asyncio
import json
import base64
from typing import TYPE_CHECKING
from livekit.agents import JobContext
from livekit.rtc.rpc import RpcInvocationData
from generated.protos import interaction_pb2

if TYPE_CHECKING:
    from main import RoxAgent

# Module-level logger for this RPC service module
logger = logging.getLogger(__name__)



class AgentInteractionService:
    """Service class for handling specialized RPC interactions with the Conductor."""

    async def _handle_interrupt_like_event(
        self,
        raw_payload: RpcInvocationData,
        source: str,
        ack_text: str = "Yes — go ahead, what's your doubt?",
    ) -> str:
        """Shared handler for interrupt-like events.
        
        - Sets caller_identity (when not browser-bot)
        - Performs optimistic interrupt acknowledgement (stop TTS, enable mic)
        - Returns a standard AgentResponse
        """
        logger.info(f"[RPC][{source}] Handling interrupt-like event")
        # Ensure subsequent UI RPCs target the student's frontend (not the browser pod)
        try:
            pid = getattr(raw_payload, 'caller_identity', None)
            if self.agent and pid and not str(pid).startswith("browser-bot-"):
                self.agent.caller_identity = pid
                logger.info(f"[RPC][{source}] caller_identity set to {pid}")
        except Exception:
            pass

        # Immediate local acknowledgement: speak quick ack, stop TTS, and enable mic
        try:
            if self.agent:
                try:
                    await self.agent.optimistic_interrupt_ack(ack_text)
                except Exception as e:
                    logger.warning(f"[RPC][{source}] Optimistic interrupt ack failed (continuing): {e}")

            response_pb = interaction_pb2.AgentResponse(
                status_message="Interrupt acknowledged. You may speak now."
            )
            return base64.b64encode(response_pb.SerializeToString()).decode('utf-8')
        except Exception as e:
            logger.error(f"[RPC][{source}] Error handling interrupt-like event: {e}", exc_info=True)
            error_response = interaction_pb2.AgentResponse(
                status_message=f"Error processing interrupt: {str(e)}"
            )
            return base64.b64encode(error_response.SerializeToString()).decode('utf-8')

    async def student_wants_to_interrupt(self, raw_payload: RpcInvocationData) -> str:
        """Handle the 'Raise Hand' button press from the student.
        
        This is triggered when the student wants to interrupt the AI's speech
        or current action to ask a question or provide input.
        
        Args:
            raw_payload: The raw RPC payload
            
        Returns:
            Base64-encoded protobuf response
        """
        logger.info("[RPC] Received student_wants_to_interrupt")
        return await self._handle_interrupt_like_event(
            raw_payload, source="raise_hand", ack_text="Yes — go ahead, what's your doubt?"
        )

    async def InvokeAgentTask(self, raw_payload: RpcInvocationData) -> str:
        """Handle generic task invocation from the frontend.
        
        Expects a base64-encoded InvokeAgentTaskRequest payload.
        Decodes, enqueues the task for processing, and returns an AgentResponse.
        """
        logger.info("[RPC] Received InvokeAgentTask")
        try:
            # Update caller identity when tasks originate from the student's frontend
            try:
                pid = getattr(raw_payload, 'caller_identity', None)
                if self.agent and pid and not str(pid).startswith("browser-bot-"):
                    self.agent.caller_identity = pid
                    logger.info(f"[RPC] caller_identity set to {pid} (from InvokeAgentTask)")
            except Exception:
                pass
            # Decode protobuf request from base64
            try:
                req_bytes = base64.b64decode(raw_payload.payload)
                request_pb = interaction_pb2.InvokeAgentTaskRequest()
                request_pb.ParseFromString(req_bytes)
            except Exception as de:
                logger.error(f"Failed to decode InvokeAgentTaskRequest: {de}")
                error_response = interaction_pb2.AgentResponse(
                    status_message=f"Invalid InvokeAgentTaskRequest: {de}",
                )
                return base64.b64encode(error_response.SerializeToString()).decode('utf-8')

            task_name = request_pb.task_name or ""
            json_payload = request_pb.json_payload or "{}"

            # NEW: Parse and normalize the JSON payload so downstream sees concrete fields
            payload_data = {}
            try:
                payload_data = json.loads(json_payload) if json_payload else {}
            except Exception as pe:
                logger.warning(f"InvokeAgentTask payload not valid JSON: {pe}")
                payload_data = {}

            # Map selected suggestion to a transcript so it's treated like typed input
            if task_name == "select_suggested_response":
                text = (payload_data.get("text") or payload_data.get("value") or "").strip()
                if text:
                    payload_data.setdefault("transcript", text)
                payload_data.setdefault("interaction_type", "suggested_response")
            else:
                # Generic fallback: if a 'text' field is present but no transcript, use it
                if "transcript" not in payload_data and isinstance(payload_data.get("text"), str):
                    payload_data["transcript"] = payload_data.get("text")

            if not self.agent:
                logger.error("Agent not available to process InvokeAgentTask")
                error_response = interaction_pb2.AgentResponse(
                    status_message="Agent not available",
                    data_payload="",
                )
                return base64.b64encode(error_response.SerializeToString()).decode('utf-8')

            # Prevent duplicate session start enqueues from frontend
            if task_name == "start_tutoring_session" and self.agent:
                if getattr(self.agent, "_session_started", False) or getattr(self.agent, "_start_task_enqueued", False):
                    logger.info("[RPC] Skipping duplicate start_tutoring_session (already started or enqueued)")
                    response_pb = interaction_pb2.AgentResponse(
                        status_message="start_tutoring_session ignored: already started or enqueued"
                    )
                    return base64.b64encode(response_pb.SerializeToString()).decode('utf-8')

            # Build and enqueue task for the Conductor's processing loop
            task = {
                "task_name": task_name,
                "caller_identity": getattr(raw_payload, 'caller_identity', None),
                **payload_data,
            }

            # Parse JSON payload to extract restored_feed_summary if present
            try:
                payload_dict = json.loads(json_payload) if json_payload else {}
            except Exception:
                payload_dict = {}
            if isinstance(payload_dict, dict) and "restored_feed_summary" in payload_dict:
                task["restored_feed_summary"] = payload_dict.get("restored_feed_summary")
                logger.info("[RPC] InvokeAgentTask contains restored_feed_summary: Yes")
            else:
                logger.info("[RPC] InvokeAgentTask contains restored_feed_summary: No")
            await self.agent._processing_queue.put(task)
            try:
                preview = {k: (v if k != 'transcript' else (v[:80] + '…' if isinstance(v, str) and len(v) > 80 else v)) for k, v in task.items() if k in ("task_name", "interaction_type", "transcript")}
                logger.info(f"Queued InvokeAgentTask: {preview}")
            except Exception:
                logger.info(f"Queued InvokeAgentTask: {task_name}")

            response_pb = interaction_pb2.AgentResponse(
                status_message=f"Task '{task_name}' enqueued",
                data_payload="",
            )
            return base64.b64encode(response_pb.SerializeToString()).decode('utf-8')
            
        except Exception as e:
            logger.error(f"Error in InvokeAgentTask: {e}", exc_info=True)
            error_response = interaction_pb2.AgentResponse(
                status_message=f"Error processing task: {str(e)}"
            )
            return base64.b64encode(error_response.SerializeToString()).decode('utf-8')

    async def student_mic_button_interrupt(self, raw_payload: RpcInvocationData) -> str:
        """Handle mic button press interruption from the student.
        
        This is triggered when the student clicks the mic button to interrupt
        the AI's speech and indicate they want to speak.
        
        Args:
            raw_payload: The raw RPC payload
            
        Returns:
            Base64-encoded protobuf response
        """
        logger.info("[RPC] Received student_mic_button_interrupt")
        return await self._handle_interrupt_like_event(
            raw_payload, source="mic_button", ack_text="Yes — go ahead, what's your doubt?"
        )

    async def student_spoke_or_acted(self, raw_payload: RpcInvocationData) -> str:
        """Handle enriched student input from the Frontend Sensor.
        
        This is the main handler that receives comprehensive information about
        what the student said or did, including contextual information like
        what they were pointing at or the state of their work.
        
        Args:
            raw_payload: The raw RPC payload containing student interaction data
            
        Returns:
            Base64-encoded protobuf response
        """
        logger.info("[RPC] Received student_spoke_or_acted")
        logger.info(f"[RPC] Raw Payload: {raw_payload.payload}")  # Log the payload for debugging
        
        try:
            if not self.agent:
                logger.error("Agent not available for processing student input")
            # The Conductor decides the task_name based on its current expectation state
            task_name = ""
            if self.agent._expected_user_input_type == "SUBMISSION":
                # The agent was waiting for a specific student submission
                task_name = "handle_submission"
            else:
                # Default state is INTERRUPTION - student spoke during AI turn
                task_name = "handle_interruption"
                logger.info("Processing as interruption")
        
            # Build comprehensive task data for the Brain
            # Since PushToTalkRequest has no fields, we work with raw payload
            # Decode transcript best-effort from base64 payload
            transcript_text = ""
            try:
                if getattr(raw_payload, 'payload', None):
                    decoded = base64.b64decode(raw_payload.payload)
                    try:
                        transcript_text = decoded.decode('utf-8', errors='ignore')
                    except Exception:
                        transcript_text = str(decoded)
            except Exception:
                transcript_text = ""
            task = {
                "task_name": task_name,
                "transcript": transcript_text,
                "caller_identity": raw_payload.caller_identity,
            }
            
            # Queue the task for Brain processing
            await self.agent._processing_queue.put(task)
            logger.info(f"Queued task '{task_name}' for Brain processing")

            response_pb = interaction_pb2.AgentResponse(
                status_message="Input processed successfully."
            )
            return base64.b64encode(response_pb.SerializeToString()).decode('utf-8')
            
        except Exception as e:
            logger.error(f"Error in student_spoke_or_acted: {e}", exc_info=True)
            error_response = interaction_pb2.AgentResponse(
                status_message=f"Error processing student input: {str(e)}"
            )
            return base64.b64encode(error_response.SerializeToString()).decode('utf-8')

    async def TestPing(self, raw_payload: RpcInvocationData) -> str:
        """Handles the initial handshake from the frontend.
        This is the definitive trigger for starting a session or handling a reconnect.
        """
        logger.info("[RPC] Received TestPing")
        try:
            # Always update the agent's target identity to the student who pinged.
            pid = getattr(raw_payload, 'caller_identity', None)
            if self.agent and pid and not str(pid).startswith("browser-bot-"):
                self.agent.caller_identity = pid
                logger.info(f"[RPC] Caller identity set to {pid} (from TestPing)")

            if not self.agent:
                logger.error("Agent not available in TestPing handler")
                error_response = interaction_pb2.AgentResponse(status_message="Agent not available")
                return base64.b64encode(error_response.SerializeToString()).decode('utf-8')

            # Check if the session has been started yet for this agent instance.
            # We use the agent's internal flag to ensure this decision is made only once.
            if not getattr(self.agent, "_session_started", False):
                # This is the FIRST ping. The session has not started. Let's start it.
                logger.info("[TestPing] First ping received. Enqueuing start_tutoring_session.")

                start_task = {
                    "task_name": "start_tutoring_session",
                    "caller_identity": pid,
                }
                await self.agent._processing_queue.put(start_task)


            else:
                # The session has ALREADY started. This ping must be from a page refresh.
                # This is where we send the "reconnect nudge" to Kamikaze.
                logger.info("[TestPing] Reconnect ping received. Enqueuing reconnect_nudge.")

                reconnect_task = {
                    "task_name": "reconnect_nudge",
                    "caller_identity": pid,
                    "interaction_type": "reconnect",
                    "transcript": "[reconnect]",
                }
                await self.agent._processing_queue.put(reconnect_task)

            # Always return a success response
            response_pb = interaction_pb2.AgentResponse(
                status_message="Pong! Conductor is alive and responding."
            )
            return base64.b64encode(response_pb.SerializeToString()).decode('utf-8')

        except Exception as e:
            logger.error(f"Error in TestPing: {e}", exc_info=True)
            error_response = interaction_pb2.AgentResponse(status_message=f"Error in ping: {str(e)}")
            return base64.b64encode(error_response.SerializeToString()).decode('utf-8')

    async def student_stopped_listening(self, raw_payload: RpcInvocationData) -> str:
        """Handle when student manually stops listening (turns off mic).
        
        This is triggered when the student clicks the mic button to turn OFF
        the microphone, indicating they're done speaking.
        
        Args:
            raw_payload: The raw RPC payload
            
        Returns:
            Base64-encoded protobuf response
        """
        logger.info("[RPC] Received student_stopped_listening")
        # Ensure subsequent UI RPCs target the student's frontend (not the browser pod)
        try:
            pid = getattr(raw_payload, 'caller_identity', None)
            if self.agent and pid and not str(pid).startswith("browser-bot-"):
                self.agent.caller_identity = pid
                logger.info(f"[RPC] caller_identity set to {pid} (from student_stopped_listening)")
        except Exception:
            pass
        
        try:
            # Process any pending transcript and disable mic state
            if self.agent:
                task = {
                    "task_name": "student_stopped_listening",
                    "caller_identity": raw_payload.caller_identity,
                    "interaction_type": "manual_stop_listening"
                }
                await self.agent._processing_queue.put(task)
                logger.info("Queued student_stopped_listening task for processing")

            response_pb = interaction_pb2.AgentResponse(
                status_message="Stop listening acknowledged. Processing your input..."
            )
            return base64.b64encode(response_pb.SerializeToString()).decode('utf-8')
            
        except Exception as e:
            logger.error(f"Error in student_stopped_listening: {e}", exc_info=True)
            error_response = interaction_pb2.AgentResponse(
                status_message=f"Error processing stop listening: {str(e)}"
            )
            return base64.b64encode(error_response.SerializeToString()).decode('utf-8')