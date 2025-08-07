# File: livekit-service/rox/rpc_services.py
# rox/rpc_services.py
"""RPC service handlers for the Rox Conductor.

This module provides specialized RPC methods that form the Conductor's "ears"
for receiving specific, meaningful events from the Frontend Sensor.
"""

import logging
import json
import base64
from typing import TYPE_CHECKING
from livekit.agents import JobContext
from livekit.rtc.rpc import RpcInvocationData
from generated.protos import interaction_pb2

if TYPE_CHECKING:
    from .main import RoxAgent

logger = logging.getLogger(__name__)


class AgentInteractionService:
    """Service class for handling specialized RPC interactions with the Conductor."""

    def __init__(self, ctx: JobContext):
        """Initialize the service with a job context.
        
        Args:
            ctx: The LiveKit job context containing room and participant info
        """
        self._ctx = ctx
        self.agent: "RoxAgent" = getattr(ctx, 'rox_agent', None)

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
        
        try:
            # Use the new dedicated interruption handler
            if self.agent:
                task = {
                    "task_name": "student_wants_to_interrupt",
                    "caller_identity": raw_payload.caller_identity,
                    "interrupt_type": "voice",
                    "interaction_type": "interruption"
                }
                # Use the new interruption handler that stops current execution and forwards immediately
                await self.agent.handle_interruption(task)
                logger.info("Handled voice interrupt with stop-and-forward logic")

            response_pb = interaction_pb2.AgentResponse(
                status_message="Interrupt acknowledged. You may speak now."
            )
            return base64.b64encode(response_pb.SerializeToString()).decode('utf-8')
            
        except Exception as e:
            logger.error(f"Error in student_wants_to_interrupt: {e}", exc_info=True)
            error_response = interaction_pb2.AgentResponse(
                status_message=f"Error processing interrupt: {str(e)}"
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
        
        try:
            # Use the new dedicated interruption handler
            if self.agent:
                task = {
                    "task_name": "student_mic_button_interrupt",
                    "caller_identity": raw_payload.caller_identity,
                    "interrupt_type": "mic_button",
                    "interaction_type": "interruption"  # This routes to /handle_interruption endpoint
                }
                # Use the new interruption handler that stops current execution and forwards immediately
                await self.agent.handle_interruption(task)
                logger.info("Handled mic button interrupt with stop-and-forward logic")

            response_pb = interaction_pb2.AgentResponse(
                status_message="Mic interrupt acknowledged. You may speak now."
            )
            return base64.b64encode(response_pb.SerializeToString()).decode('utf-8')
            
        except Exception as e:
            logger.error(f"Error in student_mic_button_interrupt: {e}", exc_info=True)
            error_response = interaction_pb2.AgentResponse(
                status_message=f"Error processing mic interrupt: {str(e)}"
            )
            return base64.b64encode(error_response.SerializeToString()).decode('utf-8')

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
                error_response = interaction_pb2.AgentResponse(
                    status_message="Agent not available",
                    success=False
                )
                return base64.b64encode(error_response.SerializeToString()).decode('utf-8')
        
            # The Conductor decides the task_name based on its current expectation state
            task_name = ""
            if self.agent._expected_user_input_type == "SUBMISSION":
                # The agent was waiting for a specific student submission
                task_name = "handle_submission"
                logger.info(f"Processing as submission: {task_name}")
            else:
                # Default state is INTERRUPTION - student spoke during AI turn
                task_name = "handle_interruption"
                logger.info("Processing as interruption")
        
            # Build comprehensive task data for the Brain
            # Since PushToTalkRequest has no fields, we work with raw payload
            task = {
                "task_name": task_name,
                "transcript": raw_payload.payload,  # Use raw payload as transcript
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
        """Handle ping requests for testing connectivity.
        
        Also queues a test task to verify end-to-end LangGraph communication.
        
        Args:
            raw_payload: The raw RPC payload
            
        Returns:
            Base64-encoded protobuf response
        """
        logger.info("[RPC] Received TestPing")
        
        try:
            # Queue session start task to trigger curriculum navigation
            if self.agent:
                session_start_task = {
                    "task_name": "start_tutoring_session",
                    "caller_identity": raw_payload.caller_identity,
                }
                await self.agent._processing_queue.put(session_start_task)
                logger.info("[TestPing] Queued session start task to trigger curriculum navigation")
            
            response = interaction_pb2.AgentResponse(
                status_message="Pong! Conductor is alive and responding. Test task queued for LangGraph."
            )
            
            return base64.b64encode(response.SerializeToString()).decode('utf-8')
            
        except Exception as e:
            logger.error(f"Error in TestPing: {e}", exc_info=True)
            error_response = interaction_pb2.AgentResponse(
                status_message=f"Error in ping: {str(e)}"
            )
            return base64.b64encode(error_response.SerializeToString()).decode('utf-8')