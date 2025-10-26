# File: tests/test_unit.py
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock

# Ensure project root is importable so we can import the 'rox' package
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rox.agent import RoxAgent
from rox.langgraph_client import LangGraphClient
from aioresponses import aioresponses

# Mark all tests in this file as async
pytestmark = pytest.mark.asyncio


# ========== RoxAgent Unit Tests ==========

def test_optimize_speech_actions_combines_consecutive_speech():
    """
    Verify that consecutive 'speak' actions are merged into one.
    """
    agent = RoxAgent()
    toolbelt = [
        {"tool_name": "speak", "parameters": {"text": "Hello."}},
        {"tool_name": "speak", "parameters": {"text": "How are you?"}},
        {"tool_name": "draw", "parameters": {}},
        {"tool_name": "speak", "parameters": {"text": "I am fine."}},
    ]

    optimized = agent._optimize_speech_actions(toolbelt)

    assert len(optimized) == 3
    assert optimized[0]["tool_name"] == "speak"
    assert optimized[0]["parameters"]["text"] == "Hello. How are you?"
    assert optimized[1]["tool_name"] == "draw"
    assert optimized[2]["tool_name"] == "speak"
    assert optimized[2]["parameters"]["text"] == "I am fine."


def test_optimize_speech_actions_handles_empty_list():
    """
    Verify it doesn't crash on an empty toolbelt.
    """
    agent = RoxAgent()
    optimized = agent._optimize_speech_actions([])
    assert optimized == []


def test_optimize_speech_actions_handles_no_speech():
    """
    Verify it returns the same list if no speech actions are present.
    """
    agent = RoxAgent()
    toolbelt = [
        {"tool_name": "draw", "parameters": {}},
        {"tool_name": "wait", "parameters": {"ms": 500}},
    ]
    optimized = agent._optimize_speech_actions(toolbelt)
    assert optimized == toolbelt


# ========== LangGraphClient Unit Tests ==========

async def test_langgraph_client_success_on_first_try():
    """
    Verify the client successfully calls the '/handle_response' endpoint
    and parses the response.
    """
    test_url = "http://fake-langgraph:8001/handle_response"
    with aioresponses() as m:
        m.post(test_url, payload={
            "delivery_plan": {"actions": [{"tool_name": "speak", "parameters": {"text": "Success!"}}]},
            "current_lo_id": "lo-123",
        })

        client = LangGraphClient()
        client.base_url = "http://fake-langgraph:8001"

        task = {"task_name": "handle_response", "transcript": "a user said something"}
        response = await client.invoke_langgraph_task(task, "user1", "curr1", "session1")

        assert response is not None
        assert response["delivery_plan"]["actions"][0]["tool_name"] == "speak"
        assert "Success!" in response["delivery_plan"]["actions"][0]["parameters"]["text"]


async def test_langgraph_client_retries_on_503_error():
    """
    Verify the client retries the request if it receives a 5xx server error.
    """
    test_url = "http://fake-langgraph:8001/handle_interruption"
    with aioresponses() as m:
        # First respond with 503, then with 200 OK
        m.post(test_url, status=503)
        m.post(test_url, status=200, payload={
            "delivery_plan": {"actions": [{"tool_name": "speak", "parameters": {"text": "Retry success!"}}]}
        })

        client = LangGraphClient()
        client.base_url = "http://fake-langgraph:8001"
        client.backoff_base = 0.01  # speed up retries for the test

        task = {"task_name": "handle_interruption", "interaction_type": "interruption"}
        response = await client.invoke_langgraph_task(task, "user1", "curr1", "session1")

        assert response is not None
        assert "Retry success!" in response["delivery_plan"]["actions"][0]["parameters"]["text"]

        # Verify two POST calls were made to the endpoint
        matching_keys = [k for k in m.requests.keys() if k[0] == "POST" and str(k[1]) == test_url]
        assert matching_keys
        total_calls = sum(len(m.requests[k]) for k in matching_keys)
        assert total_calls == 2


async def test_langgraph_client_400_bad_request_no_retry():
    """
    Verify the client does not retry on 4xx and returns None.
    """
    test_url = "http://fake-langgraph:8001/handle_response"
    with aioresponses() as m:
        # Single 400 response
        m.post(test_url, status=400, body="Bad Request")

        client = LangGraphClient()
        client.base_url = "http://fake-langgraph:8001"
        client.max_retries = 5

        task = {"task_name": "handle_response", "transcript": "oops"}
        response = await client.invoke_langgraph_task(task, "user1", "curr1", "session1")

        assert response is None
        matching_keys = [k for k in m.requests.keys() if k[0] == "POST" and str(k[1]) == test_url]
        assert matching_keys
        total_calls = sum(len(m.requests[k]) for k in matching_keys)
        assert total_calls == 1  # no retry on 400


async def test_langgraph_client_missing_delivery_plan_returns_none():
    """
    If response JSON lacks 'delivery_plan', the client should return None.
    """
    test_url = "http://fake-langgraph:8001/handle_response"
    with aioresponses() as m:
        m.post(test_url, payload={"message": "ok but no plan"})

        client = LangGraphClient()
        client.base_url = "http://fake-langgraph:8001"

        task = {"task_name": "handle_response", "transcript": "hello"}
        response = await client.invoke_langgraph_task(task, "user1", "curr1", "session1")

        assert response is None
