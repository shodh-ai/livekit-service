# Tests: RoxAgent._execute_toolbelt handles get_block_content by requesting frontend and enqueuing a follow-up task

import asyncio
import sys
from pathlib import Path
import types

import pytest

THIS_DIR = Path(__file__).resolve().parent
ROX_DIR = THIS_DIR.parent
if str(ROX_DIR) not in sys.path:
    sys.path.insert(0, str(ROX_DIR))

from agent import RoxAgent  # noqa: E402


class StubFrontendClient:
    def __init__(self, data):
        self._data = data
        self.calls = []

    async def get_block_content_from_frontend(self, room, identity, block_id, timeout_sec: float = 15.0):
        self.calls.append((room, identity, block_id, timeout_sec))
        return self._data


@pytest.mark.asyncio
async def test_execute_toolbelt_get_block_content_enqueues_followup_task():
    # Create a RoxAgent instance but manually set attributes we need
    agent = RoxAgent.__new__(RoxAgent)  # bypass base class __init__
    agent._is_paused = False
    agent._current_execution_cancelled = False
    agent._current_delivery_plan = None
    agent._current_plan_index = 0
    agent._room = object()  # any truthy sentinel
    agent.caller_identity = "student-1"
    agent._processing_queue = asyncio.Queue()
    stub_data = {"id": "block-1", "type": "excalidraw", "elements": []}
    agent._frontend_client = StubFrontendClient(stub_data)

    toolbelt = [
        {"tool_name": "get_block_content", "parameters": {"block_id": "block-1"}}
    ]

    await agent._execute_toolbelt(toolbelt)

    # The stub should have been called
    assert agent._frontend_client.calls, "Expected a frontend RPC call"
    # A follow-up task should be queued for Brain with block_id/content
    queued = await asyncio.wait_for(agent._processing_queue.get(), timeout=1)
    assert queued["task_name"] == "handle_response"
    assert queued["interaction_type"] == "block_content"
    assert queued["block_id"] == "block-1"
    assert isinstance(queued.get("block_content"), dict)
