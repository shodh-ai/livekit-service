import asyncio
import os
import signal
import sys
import time
import subprocess

import pytest
from unittest.mock import AsyncMock, MagicMock

from rox.agent import RoxAgent

pytestmark = pytest.mark.asyncio

@pytest.fixture(scope="module")
def mock_brain_server():
    env = os.environ.copy()
    cmd = [sys.executable, "-m", "uvicorn", "rox.tests.mock_brain:app", "--host", "127.0.0.1", "--port", "8001", "--log-level", "warning"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    # Wait a moment for server to start
    time.sleep(1.0)
    yield "http://127.0.0.1:8001"
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
    except Exception:
        pass


async def test_e2e_start_session_against_mock_brain(mock_brain_server):
    agent = RoxAgent()
    # Fake frontend and room/session
    agent._frontend_client = MagicMock()
    agent._frontend_client.execute_visual_action = AsyncMock()
    agent.agent_session = MagicMock()
    agent.agent_session.say = AsyncMock(side_effect=lambda *a, **k: asyncio.sleep(0))
    agent._room = MagicMock()

    # Provide identifiers required by the mock brain schema
    agent.session_id = "session-e2e"
    agent.user_id = "student-e2e"
    agent.curriculum_id = "curriculum-e2e"

    # Point client to mock brain
    agent._langgraph_client.base_url = mock_brain_server

    # Enqueue start task
    await agent._processing_queue.put({"task_name": "start_tutoring_session", "caller_identity": "student-e2e"})

    loop_task = asyncio.create_task(agent.processing_loop())
    await asyncio.sleep(0.3)

    agent.agent_session.say.assert_any_call("Hello from the Mock Brain!", allow_interruptions=True)

    loop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop_task
