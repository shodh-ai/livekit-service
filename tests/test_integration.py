# File: tests/test_integration.py
import pytest
import asyncio
import json
import base64
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure project root is importable
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rox.agent import RoxAgent
from rox.rpc_services import AgentInteractionService
from rox.generated.protos import interaction_pb2

# Mark all tests in this file as async
pytestmark = pytest.mark.asyncio


@pytest.fixture
def agent():
    """A pytest fixture to create a fresh RoxAgent for each test with safe mocks."""
    agent_instance = RoxAgent()
    # Mock FrontendClient
    agent_instance._frontend_client = MagicMock()
    agent_instance._frontend_client.set_mic_enabled = AsyncMock()
    agent_instance._frontend_client.execute_visual_action = AsyncMock()
    # Mock AgentSession.say to return an awaitable (like a playback handle)
    agent_instance.agent_session = MagicMock()
    agent_instance.agent_session.say = AsyncMock(side_effect=lambda *a, **k: asyncio.sleep(0))
    # Provide a dummy room so calls carry an object
    agent_instance._room = MagicMock()
    return agent_instance


async def test_rpc_invokeagenttask_queues_task_correctly(agent):
    """
    Verify that calling the InvokeAgentTask RPC method correctly
    parses the payload and puts a well-formed task onto the agent's queue.
    """
    service = AgentInteractionService()
    service.agent = agent

    task_request = interaction_pb2.InvokeAgentTaskRequest(
        task_name="start_tutoring_session",
        json_payload=json.dumps({"user_name": "test_student"})
    )
    encoded_payload = base64.b64encode(task_request.SerializeToString()).decode('utf-8')

    mock_invocation = MagicMock()
    mock_invocation.payload = encoded_payload
    mock_invocation.caller_identity = "student-id-123"

    await service.InvokeAgentTask(mock_invocation)

    assert agent._processing_queue.qsize() == 1

    queued_task = await agent._processing_queue.get()
    assert queued_task["task_name"] == "start_tutoring_session"
    assert queued_task["caller_identity"] == "student-id-123"
    assert queued_task["user_name"] == "test_student"


@patch('rox.agent.LangGraphClient')
async def test_full_interaction_loop(MockLangGraphClient, agent):
    """
    Simulates a full interaction:
    1. An RPC call puts a task on the queue.
    2. The agent's processing loop picks it up.
    3. The loop calls a mocked LangGraphClient.
    4. The loop executes the delivery plan from the mock response.
    """
    mock_langgraph_instance = MockLangGraphClient.return_value
    mock_langgraph_instance.invoke_langgraph_task = AsyncMock(return_value={
        "delivery_plan": {
            "actions": [
                {"tool_name": "speak", "parameters": {"text": "Hello from the test!"}},
                {"tool_name": "draw", "parameters": {"shape": "circle"}},
            ]
        },
        "current_lo_id": "lo-abc"
    })
    agent._langgraph_client = mock_langgraph_instance

    initial_task = {"task_name": "start_tutoring_session", "caller_identity": "student-xyz"}
    await agent._processing_queue.put(initial_task)

    loop_task = asyncio.create_task(agent.processing_loop())

    await asyncio.sleep(0.1)

    mock_langgraph_instance.invoke_langgraph_task.assert_called_once()
    call_args, _ = mock_langgraph_instance.invoke_langgraph_task.call_args
    assert call_args[0]['task_name'] == 'start_tutoring_session'

    agent.agent_session.say.assert_any_call("Hello from the test!", allow_interruptions=True)

    agent._frontend_client.execute_visual_action.assert_any_call(
        agent._room,
        agent.caller_identity,
        "draw",
        {"shape": "circle"}
    )

    loop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop_task
