# rpc_services.py (FINAL, SIMPLIFIED VERSION)

import logging
import json
import base64
import asyncio

from livekit.rtc.rpc import RpcInvocationData
from generated.protos import interaction_pb2
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from main import RoxAgent

logger = logging.getLogger(__name__)

class AgentInteractionService:
    """
    Handles all incoming RPCs from the frontend.
    Its only job is to delegate tasks to the active RoxAgent instance.
    """
    def __init__(self, agent: 'RoxAgent'):
        self._agent = agent

    async def InvokeAgentTask(self, raw_payload: RpcInvocationData) -> str:
        """The universal RPC for triggering any LangGraph task."""
        logger.info("[RPC] InvokeAgentTask received.")
        request = interaction_pb2.InvokeAgentTaskRequest()
        request.ParseFromString(base64.b64decode(raw_payload.payload))
        
        # Immediately queue the task without waiting for it to complete.
        await self._agent.trigger_langgraph_task(
            task_name=request.task_name,
            json_payload=request.json_payload,
            caller_identity=raw_payload.caller_identity
        )
        
        response_pb = interaction_pb2.AgentResponse(status_message=f"Task '{request.task_name}' acknowledged.")
        return base64.b64encode(response_pb.SerializeToString()).decode('utf-8')

    async def RequestInterrupt(self, raw_payload: RpcInvocationData) -> str:
        """
        Handles the 'Raise Hand' button press. This is a high-priority control signal.
        """
        logger.info("[RPC] RequestInterrupt received.")
        
        # 1. Immediately interrupt any ongoing speech.
        if self._agent.agent_session:
            self._agent.agent_session.interrupt()
            logger.warning("Interruption signal received. TTS interrupted.")

        # 2. Queue the special acknowledgment task in LangGraph.
        # This task will make the agent say "What's on your mind?" and then listen.
        await self._agent.trigger_langgraph_task(
            task_name="acknowledge_interruption",
            json_payload=json.dumps({"user_id": raw_payload.caller_identity}),
            caller_identity=raw_payload.caller_identity
        )
        
        response_pb = interaction_pb2.AgentResponse(status_message="Interrupt acknowledged.")
        return base64.b64encode(response_pb.SerializeToString()).decode('utf-8')

    # You can keep other RPCs like TestPing if you use them for simple debugging.
    async def TestPing(self, raw_payload: RpcInvocationData) -> str:
        logger.info(f"[RPC] TestPing received from {raw_payload.caller_identity}.")
        return base64.b64encode(interaction_pb2.AgentResponse(status_message="Pong!").SerializeToString()).decode('utf-8')