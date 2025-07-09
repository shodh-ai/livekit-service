# rpc_services.py (FINAL, PRODUCTION-READY VERSION)

import logging
import json
import base64
import asyncio
from typing import TYPE_CHECKING

from livekit.agents import JobContext
from livekit.rtc.rpc import RpcInvocationData
from generated.protos import interaction_pb2

if TYPE_CHECKING:
    from main import RoxAgent

logger = logging.getLogger(__name__)

class AgentInteractionService:
    def __init__(self, ctx: JobContext):
        self._ctx = ctx
        # self.agent_instance is now findable via the context
        self.agent_instance: "RoxAgent" = ctx.rox_agent

    async def InvokeAgentTask(self, raw_payload: RpcInvocationData) -> str:
        """The universal RPC for triggering any LangGraph task."""
        logger.info(f"[RPC] InvokeAgentTask received for task.")
        request = interaction_pb2.InvokeAgentTaskRequest()
        request.ParseFromString(base64.b64decode(raw_payload.payload))

        # --- THIS IS THE NEW, SAFE LOGIC ---
        # Before queuing the task, inspect the payload and cache the IDs on the agent instance.
        # This is safe because this is the first place the data arrives from the client.
        try:
            payload_dict = json.loads(request.json_payload)
            context = payload_dict.get("current_context", {})

            if context.get("user_id"):
                self.agent_instance.user_id = context.get("user_id")
                logger.info(f"Cached user_id: {self.agent_instance.user_id}")
            if context.get("session_id"):
                self.agent_instance.session_id = context.get("session_id")
                logger.info(f"Cached session_id: {self.agent_instance.session_id}")
            
            # Also cache the identity of the client who made the call
            self.agent_instance.caller_identity = raw_payload.caller_identity
            logger.info(f"Cached caller_identity: {self.agent_instance.caller_identity}")

        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Could not parse context from incoming payload to cache IDs: {e}")
            # We don't stop the flow, as the IDs might already be cached from a previous call.
        # --- END OF NEW LOGIC ---

        # Now, queue the task as before.
        await self.agent_instance.trigger_langgraph_task(
            task_name=request.task_name,
            json_payload=request.json_payload,
            caller_identity=raw_payload.caller_identity
        )

        response_pb = interaction_pb2.AgentResponse(status_message=f"Task '{request.task_name}' acknowledged.")
        return base64.b64encode(response_pb.SerializeToString()).decode('utf-8')

    async def RequestInterrupt(self, raw_payload: RpcInvocationData) -> str:
        """Handles the 'Raise Hand' button press. This is a high-priority control signal."""
        logging.info("RPC: Received RequestInterrupt.")
        if self.agent_instance and self.agent_instance.agent_session:
            # This call is what triggers the TTSPlaybackCancelled exception
            self.agent_instance.agent_session.interrupt()
            logger.warning("Interruption signal sent to agent session.")

        response_pb = interaction_pb2.AgentResponse(status_message="Interrupt acknowledged.")
        return base64.b64encode(response_pb.SerializeToString()).decode('utf-8')

    async def TestPing(self, raw_payload: RpcInvocationData) -> str:
        logger.info(f"[RPC] TestPing received from {raw_payload.caller_identity}.")
        return base64.b64encode(interaction_pb2.AgentResponse(status_message="Pong!").SerializeToString()).decode('utf-8')

    async def UpdateAgentContext(self, raw_payload: RpcInvocationData) -> str:
        """Receives context updates from the frontend and merges them into the agent's state."""
        logger.info("[RPC] UpdateAgentContext received.")
        request = interaction_pb2.UpdateAgentContextRequest()
        request.ParseFromString(base64.b64decode(raw_payload.payload))

        try:
            incoming_context = json.loads(request.json_context_payload)
            if not isinstance(incoming_context, dict):
                raise ValueError("Payload is not a dictionary.")

            # Ensure the agent_context dictionary exists
            if not hasattr(self.agent_instance, 'agent_context'):
                self.agent_instance.agent_context = {}
            
            # Merge the new context
            self.agent_instance.agent_context.update(incoming_context)
            logger.info(f"Agent context updated: {self.agent_instance.agent_context}")

            response_pb = interaction_pb2.AgentResponse(status_message="Context updated successfully.")
        except (json.JSONDecodeError, ValueError) as e:
            logger.error(f"Failed to update agent context: {e}")
            response_pb = interaction_pb2.AgentResponse(status_message=f"Error updating context: {e}")

        return base64.b64encode(response_pb.SerializeToString()).decode('utf-8')