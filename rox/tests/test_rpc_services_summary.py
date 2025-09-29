# Tests: rpc_services.InvokeAgentTask should parse restored_feed_summary and include it in queued task

import asyncio
import base64
import json
from pathlib import Path
import sys
import types

import pytest

# Ensure we can import modules from rox/
THIS_DIR = Path(__file__).resolve().parent
ROX_DIR = THIS_DIR.parent
if str(ROX_DIR) not in sys.path:
    sys.path.insert(0, str(ROX_DIR))

from rpc_services import AgentInteractionService  # noqa: E402
from generated.protos import interaction_pb2  # noqa: E402


class FakeJobCtx:
    def __init__(self, agent):
        self.rox_agent = agent


class FakeAgent:
    def __init__(self):
        self._processing_queue = asyncio.Queue()


class FakeRpcInvocation:
    def __init__(self, payload: str, caller_identity: str = "student-1"):
        self.payload = payload
        self.caller_identity = caller_identity


@pytest.mark.asyncio
async def test_invoke_agent_task_forwards_restored_feed_summary():
    agent = FakeAgent()
    service = AgentInteractionService(FakeJobCtx(agent))

    # Build InvokeAgentTaskRequest with restored_feed_summary
    req = interaction_pb2.InvokeAgentTaskRequest(
        task_name="start_tutoring_session",
        json_payload=json.dumps({
            "restored_feed_summary": {
                "blocks": [
                    {"id": "block-1", "type": "excalidraw", "elements_count": 3}
                ]
            }
        }),
    )
    payload_b64 = base64.b64encode(req.SerializeToString()).decode("utf-8")
    raw = FakeRpcInvocation(payload=payload_b64)

    # Call handler
    resp_b64 = await service.InvokeAgentTask(raw)
    assert isinstance(resp_b64, str)

    # One task should be queued
    queued = await asyncio.wait_for(agent._processing_queue.get(), timeout=1)
    assert queued["task_name"] == "start_tutoring_session"
    # The handler should have parsed and included restored_feed_summary
    assert "restored_feed_summary" in queued
    assert queued["restored_feed_summary"]["blocks"][0]["id"] == "block-1"

    # Response decodes to AgentResponse
    resp_bytes = base64.b64decode(resp_b64)
    resp = interaction_pb2.AgentResponse()
    resp.ParseFromString(resp_bytes)
    assert "enqueued" in resp.status_message.lower()
