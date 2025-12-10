import pytest
from unittest.mock import AsyncMock, MagicMock

from rox.agent import RoxAgent


@pytest.mark.asyncio
async def test_execute_speak_calls_agent_session():
    agent = RoxAgent()
    agent.agent_session = MagicMock()
    agent.agent_session.say = AsyncMock()

    params = {"text": "   Hello, world!   "}

    await agent._execute_speak(params)

    agent.agent_session.say.assert_called_once_with("Hello, world!", allow_interruptions=True)
