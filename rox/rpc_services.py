# rpc_services.py (CORRECTED AND FINAL VERSION)

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

    # This helper is now NON-BLOCKING because the RPC handlers will no longer await it.
    def _dispatch_task(self, task_name: str, json_payload: str, caller_identity: str):
        """A helper to trigger the LangGraph task in the background."""
        if self.agent_instance:
            # This correctly creates a background task without being awaited.
            asyncio.create_task(
                self.agent_instance.trigger_langgraph_task(
                    task_name, json_payload, caller_identity
                )
            )
        else:
            logger.error(
                f"Cannot dispatch task '{task_name}': Agent instance is not available."
            )

    # This RPC handler remains the same, but the internal call is now non-blocking
    async def InvokeAgentTask(self, raw_payload: RpcInvocationData) -> str:
        logger.info("[RPC] InvokeAgentTask invoked.")
        request = interaction_pb2.InvokeAgentTaskRequest()
        request.ParseFromString(base64.b64decode(raw_payload.payload))

        # We call the helper, which does not block.
        self._dispatch_task(
            request.task_name, request.json_payload, raw_payload.caller_identity
        )

        response_pb = interaction_pb2.AgentResponse(
            status_message=f"Task '{request.task_name}' acknowledged."
        )
        # This return happens immediately.
        return base64.b64encode(response_pb.SerializeToString()).decode("utf-8")

    # +++ FIX APPLIED HERE +++
    async def HandleFrontendButton(self, raw_payload: RpcInvocationData) -> str:
        logger.info("[RPC] HandleFrontendButton invoked (as wrapper).")
        request = interaction_pb2.FrontendButtonClickRequest()
        request.ParseFromString(base64.b64decode(raw_payload.payload))

        # THE FIX: We are NO LONGER awaiting the dispatch.
        # This call now runs in the background.
        self._dispatch_task(
            request.button_id, request.custom_data, raw_payload.caller_identity
        )

        response_pb = interaction_pb2.AgentResponse(
            status_message=f"Button '{request.button_id}' acknowledged."
        )
        # This return happens immediately, unblocking the client.
        return base64.b64encode(response_pb.SerializeToString()).decode("utf-8")

    # +++ FIX APPLIED HERE +++
    async def HandlePushToTalk(self, raw_payload: RpcInvocationData) -> str:
        """
        Handles the push-to-talk request by immediately returning and
        dispatching the actual work to a background task.
        """
        caller_id = raw_payload.caller_identity
        logger.info(
            f"[RPC] HandlePushToTalk invoked by '{caller_id}'. Dispatching to background."
        )

        try:
            if self.agent_instance:
                # THE FIX: DO NOT AWAIT. This runs pause_main_activity in the background.
                # The RPC handler does not wait for it to finish.
                asyncio.create_task(self.agent_instance.pause_main_activity())

            # This code now runs INSTANTLY after creating the background task.
            status_msg = "Acknowledged. Doubt sequence initiated."
            logger.info(status_msg)
            response_pb = interaction_pb2.AgentResponse(status_message=status_msg)
            # The response is sent back to the frontend immediately.
            return base64.b64encode(response_pb.SerializeToString()).decode("utf-8")

        except Exception as e:
            logger.error(f"Error processing HandlePushToTalk: {e}", exc_info=True)
            response_pb = interaction_pb2.AgentResponse(
                status_message=f"Error on server: {e}"
            )
            return base64.b64encode(response_pb.SerializeToString()).decode("utf-8")

    async def NotifyPageLoadV2(self, raw_payload: RpcInvocationData) -> str:
        logger.info("[RPC] NotifyPageLoadV2 invoked.")
        request = interaction_pb2.NotifyPageLoadRequest()
        request.ParseFromString(base64.b64decode(raw_payload.payload))

        task_name = "handle_page_load"
        json_payload = json.dumps({"..."})  # Your existing data

        # THE FIX: We are NO LONGER awaiting the dispatch.
        self._dispatch_task(task_name, json_payload, raw_payload.caller_identity)

        response_data = {"status": "success", "message": "Page load acknowledged."}
        response_pb = interaction_pb2.AgentResponse(
            status_message="Page load processed.",
            data_payload=json.dumps(response_data),
        )
        return base64.b64encode(response_pb.SerializeToString()).decode("utf-8")

    async def TestPing(self, raw_payload: RpcInvocationData) -> str:
        logger.info(f"[RPC] TestPing received from {raw_payload.caller_identity}.")
        response_pb = interaction_pb2.AgentResponse(status_message="Pong!")
        return base64.b64encode(response_pb.SerializeToString()).decode("utf-8")
