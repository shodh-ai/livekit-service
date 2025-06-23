# rpc_services.py (CLEANED UP AND FINAL VERSION)

import logging
import json
import base64
import asyncio

from livekit.rtc.rpc import RpcInvocationData
from generated.protos import interaction_pb2

logger = logging.getLogger(__name__)

class AgentInteractionService:
    def __init__(self, agent_instance=None):
        self.agent_instance = agent_instance
        logger.info("AgentInteractionService initialized.")

    async def _dispatch_task(self, task_name: str, json_payload: str, caller_identity: str):
        """A helper to asynchronously trigger the LangGraph task."""
        if self.agent_instance:
            logger.info(f"Dispatching task '{task_name}' to LangGraph for caller '{caller_identity}'.")
            asyncio.create_task(
                self.agent_instance.trigger_langgraph_task(task_name, json_payload, caller_identity)
            )
        else:
            logger.error(f"Cannot dispatch task '{task_name}': Agent instance is not available.")

    async def InvokeAgentTask(self, raw_payload: RpcInvocationData) -> str:
        logger.info("[RPC] InvokeAgentTask invoked.")
        request = interaction_pb2.InvokeAgentTaskRequest()
        request.ParseFromString(base64.b64decode(raw_payload.payload))
        
        await self._dispatch_task(request.task_name, request.json_payload, raw_payload.caller_identity)
        
        response_pb = interaction_pb2.AgentResponse(status_message=f"Task '{request.task_name}' acknowledged.")
        return base64.b64encode(response_pb.SerializeToString()).decode('utf-8')

    async def HandleFrontendButton(self, raw_payload: RpcInvocationData) -> str:
        logger.info("[RPC] HandleFrontendButton invoked (as wrapper).")
        request = interaction_pb2.FrontendButtonClickRequest()
        request.ParseFromString(base64.b64decode(raw_payload.payload))
        
        # The button_id becomes the task_name. The custom_data is the JSON payload.
        await self._dispatch_task(request.button_id, request.custom_data, raw_payload.caller_identity)

        response_pb = interaction_pb2.AgentResponse(status_message=f"Button '{request.button_id}' acknowledged.")
        return base64.b64encode(response_pb.SerializeToString()).decode('utf-8')

    async def NotifyPageLoadV2(self, raw_payload: RpcInvocationData) -> str:
        logger.info("[RPC] NotifyPageLoadV2 invoked.")
        request = interaction_pb2.NotifyPageLoadRequest()
        request.ParseFromString(base64.b64decode(raw_payload.payload))
        
        # Convert the page load into a generic task for LangGraph
        task_name = "handle_page_load"
        json_payload = json.dumps({
            "user_id": request.user_id,
            "session_id": request.session_id,
            "current_page": request.current_page,
            "task_stage": request.task_stage,
            "chat_history": list(request.chat_history),
        })
        
        await self._dispatch_task(task_name, json_payload, raw_payload.caller_identity)
        
        response_data = {"status": "success", "message": "Page load acknowledged."}
        response_pb = interaction_pb2.AgentResponse(
            status_message="Page load processed.",
            data_payload=json.dumps(response_data)
        )
        return base64.b64encode(response_pb.SerializeToString()).decode('utf-8')

    async def TestPing(self, raw_payload: RpcInvocationData) -> str:
        logger.info(f"[RPC] TestPing received from {raw_payload.caller_identity}.")
        response_pb = interaction_pb2.AgentResponse(status_message="Pong!")
        return base64.b64encode(response_pb.SerializeToString()).decode('utf-8')