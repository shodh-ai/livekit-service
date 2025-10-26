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


@pytest.mark.asyncio
async def test_execute_browser_command_calls_pod_client():
    agent = RoxAgent()
    agent._browser_pod_client = MagicMock()
    agent._browser_pod_client.send_browser_command = AsyncMock()
    agent.session_id = "test-session-123"
    agent._room = MagicMock()

    tool_name = "browser_navigate"
    params = {"url": "https://example.com"}

    await agent._execute_browser_command(tool_name, params)

    agent._browser_pod_client.send_browser_command.assert_called_once_with(
        agent._room,
        "browser-bot-test-session-123",
        "browser_navigate",
        {"url": "https://example.com"},
    )
