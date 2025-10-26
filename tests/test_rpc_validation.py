import base64
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from rox.rpc_services import AgentInteractionService
from rox.generated.protos import interaction_pb2

pytestmark = pytest.mark.asyncio


async def test_invoke_agent_task_invalid_json_returns_error_response():
    service = AgentInteractionService()
    service.agent = MagicMock()

    bad_json = "{not: valid json}"
    req = interaction_pb2.InvokeAgentTaskRequest(
        task_name="handle_response",
        json_payload=bad_json,
    )
    payload_b64 = base64.b64encode(req.SerializeToString()).decode("utf-8")

    inv = MagicMock()
    inv.payload = payload_b64
    inv.caller_identity = "student-x"

    resp_b64 = await service.InvokeAgentTask(inv)
    resp_bytes = base64.b64decode(resp_b64)
    resp = interaction_pb2.AgentResponse()
    resp.ParseFromString(resp_bytes)

    assert "Invalid JSON payload" in resp.status_message
