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

        # Queue the task for the agent to process, but don't wait for it.
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