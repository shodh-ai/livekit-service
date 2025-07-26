# File: livekit-service/tests/test_conductor.py
# tests/test_conductor.py
"""
Integration tests for the Conductor architecture.

These tests verify that the Conductor correctly:
1. Receives RPC calls and queues tasks
2. Processes tasks by calling the Brain (LangGraph)
3. Executes toolbelts by calling the Frontend
4. Manages State of Expectation correctly
"""

import asyncio
import pytest
import pytest_asyncio
import json
import base64
from unittest.mock import AsyncMock, MagicMock, patch

# Import the classes we need to test and mock
from rox.main import RoxAgent
from rox.rpc_services import AgentInteractionService
from rox.langgraph_client import LangGraphClient
from rox.frontend_client import FrontendClient
from generated.protos import interaction_pb2

# Marks all tests in this file as async
pytestmark = pytest.mark.asyncio

@pytest_asyncio.fixture
async def conductor_services():
    """Fixture to provide mocked Conductor services for testing."""
    # Create a mock LiveKit context
    mock_ctx = MagicMock()
    
    # Create the RoxAgent (Conductor) with mocked dependencies
    agent = RoxAgent()
    agent._room = MagicMock()
    agent._room.local_participant = MagicMock()
    # Create a proper base64-encoded mock response
    mock_response = interaction_pb2.ClientUIActionResponse(
        request_id="test-request",
        success=True,
        message="Mock action completed"
    )
    mock_response_b64 = base64.b64encode(mock_response.SerializeToString()).decode('utf-8')
    agent._room.local_participant.perform_rpc = AsyncMock(return_value=mock_response_b64)
    agent.caller_identity = "test_client_123"
    agent.user_id = "test_user"
    agent.session_id = "test_session"
    
    # Create the RPC service with proper context
    mock_ctx.rox_agent = agent
    rpc_service = AgentInteractionService(ctx=mock_ctx)
    
    return agent, rpc_service, mock_ctx

class TestConductorRPCHandlers:
    """Test the Conductor's RPC 'ears' - how it receives and processes incoming calls."""

    async def test_student_wants_to_interrupt(self, conductor_services):
        """
        Tests the "Hand Raise" flow: RPC -> Queue -> Brain -> Frontend
        """
        agent, rpc_service, mock_ctx = conductor_services
        
        # --- 1. Mock the External Services ---
        
        # Mock the Brain: When invoke_langgraph_task is called, pretend the Brain
        # returned a hardcoded "acknowledger" toolbelt.
        mock_brain_response = [
            {"tool_name": "speak", "parameters": {"text": "Of course, what's on your mind?"}},
            {"tool_name": "set_ui_state", "parameters": {"is_student_turn": True}}
        ]
        
        # Mock the agent's LangGraph client instance directly
        agent._langgraph_client.invoke_langgraph_task = AsyncMock(return_value=mock_brain_response)
        
        # Mock the TTS engine
        agent.agent_session = MagicMock()
        agent.agent_session.say = AsyncMock()
        agent.agent_session.interrupt = MagicMock()
        
        # --- 2. Simulate the Trigger ---
        # Pretend the Frontend Sensor called the RPC service
        mock_rpc_payload = MagicMock()
        mock_rpc_payload.caller_identity = "test_client_123"
        
        response = await rpc_service.student_wants_to_interrupt(mock_rpc_payload)
        
        # --- 3. Verify the Immediate Effects ---
        # Check if the agent session was interrupted
        agent.agent_session.interrupt.assert_called_once()
        
        # Check if a task was correctly placed on the queue
        assert agent._processing_queue.qsize() == 1
        
        # Verify the response
        response_pb = interaction_pb2.AgentResponse()
        response_pb.ParseFromString(base64.b64decode(response))
        assert "interrupt acknowledged" in response_pb.status_message.lower()
        
        # --- 4. Run the Conductor's Logic ---
        # Process one task from the queue
        task = await agent._processing_queue.get()
        
        # Simulate the processing loop handling this task
        toolbelt = await agent._langgraph_client.invoke_langgraph_task(
            task=task,
            user_id=agent.user_id or "anonymous",
            session_id=agent.session_id or "default_session"
        )
        
        await agent._execute_toolbelt(toolbelt)
        
        # --- 5. Verify the Final Outcome ---
        # Check that the Brain was called with the correct task
        agent._langgraph_client.invoke_langgraph_task.assert_called_once()
        call_args = agent._langgraph_client.invoke_langgraph_task.call_args
        assert call_args[1]['task']['task_name'] == 'student_wants_to_interrupt'
        
        # Check that the Conductor correctly executed the Brain's script
        agent.agent_session.say.assert_called_once_with(text="Of course, what's on your mind?", allow_interruptions=True)

    async def test_student_spoke_or_acted_interruption(self, conductor_services):
        """
        Tests the interruption flow when agent is in INTERRUPTION expectation state.
        """
        agent, rpc_service, mock_ctx = conductor_services
        
        # --- 1. Setup the "State of Expectation" ---
        # Agent is in default INTERRUPTION state
        assert agent._expected_user_input_type == "INTERRUPTION"
        
        # --- 2. Mock the Brain ---
        mock_brain_response = [
            {"tool_name": "speak", "parameters": {"text": "I understand your question. Let me help."}}
        ]
        agent._langgraph_client.invoke_langgraph_task = AsyncMock(return_value=mock_brain_response)
        
        # --- 3. Simulate the Trigger ---
        # Mock the RPC payload directly (no need for StudentSpokeOrActedRequest)
        mock_rpc_payload = MagicMock()
        mock_rpc_payload.caller_identity = "test_client_123"
        mock_rpc_payload.payload = "I don't understand this part"
        
        response = await rpc_service.student_spoke_or_acted(mock_rpc_payload)
        
        # --- 4. Verify the Outcome ---
        # Check that a task was queued with the correct task_name for interruption
        assert agent._processing_queue.qsize() == 1
        
        queued_task = await agent._processing_queue.get()
        assert queued_task['task_name'] == "handle_interruption"
        assert queued_task['transcript'] == "I don't understand this part"
        assert queued_task['caller_identity'] == "test_client_123"
        
        # Verify the response
        response_pb = interaction_pb2.AgentResponse()
        response_pb.ParseFromString(base64.b64decode(response))
        assert "processed successfully" in response_pb.status_message.lower()

    async def test_student_spoke_or_acted_submission(self, conductor_services):
        """
        Tests the submission flow when agent is in SUBMISSION expectation state.
        """
        agent, rpc_service, mock_ctx = conductor_services
        
        # --- 1. Setup the "State of Expectation" ---
        # Pretend the AI just finished a scaffolding task and is waiting for submission
        agent._expected_user_input_type = "SUBMISSION"
        
        # --- 2. Mock the Brain ---
        mock_brain_response = [
            {"tool_name": "speak", "parameters": {"text": "Great work on your submission!"}}
        ]
        agent._langgraph_client.invoke_langgraph_task = AsyncMock(return_value=mock_brain_response)
        
        # --- 3. Simulate the Trigger ---
        # Mock the RPC payload directly (no need for StudentSpokeOrActedRequest)
        mock_rpc_payload = MagicMock()
        mock_rpc_payload.caller_identity = "test_client_123"
        mock_rpc_payload.payload = "I fixed the equation"
        
        await rpc_service.student_spoke_or_acted(mock_rpc_payload)
        
        # --- 4. Verify the Outcome ---
        # Check that the Brain was called with the correct task name for submission
        assert agent._processing_queue.qsize() == 1
        
        queued_task = await agent._processing_queue.get()
        assert queued_task['task_name'] == "handle_submission"
        assert queued_task['transcript'] == "I fixed the equation"
        assert queued_task['caller_identity'] == "test_client_123"

class TestConductorActionExecutor:
    """Test the Conductor's Unified Action Executor - how it performs toolbelt scripts."""

    async def test_execute_speak_action(self, conductor_services):
        """Test that the Conductor correctly executes 'speak' actions."""
        agent, rpc_service, mock_ctx = conductor_services
        
        # Mock the TTS engine
        agent.agent_session = MagicMock()
        agent.agent_session.say = AsyncMock()
        
        # Create a toolbelt with a speak action
        toolbelt = [
            {"tool_name": "speak", "parameters": {"text": "Hello, student!"}}
        ]
        
        # Execute the toolbelt
        await agent._execute_toolbelt(toolbelt)
        
        # Verify the TTS was called correctly
        agent.agent_session.say.assert_called_once_with(text="Hello, student!", allow_interruptions=True)

    async def test_execute_ui_state_action(self, conductor_services):
        """Test that the Conductor correctly executes 'set_ui_state' actions."""
        agent, rpc_service, mock_ctx = conductor_services
        
        # Mock the agent's frontend client instance directly
        mock_set_ui_state = AsyncMock()
        agent._frontend_client.set_ui_state = mock_set_ui_state
        
        # Create a toolbelt with a UI state action
        toolbelt = [
            {"tool_name": "set_ui_state", "parameters": {"mode": "drawing", "tool": "pen"}}
        ]
        
        # Execute the toolbelt
        await agent._execute_toolbelt(toolbelt)
        
        # Verify the frontend client was called correctly
        mock_set_ui_state.assert_called_once_with(
            agent._room,
            agent.caller_identity,
            {"mode": "drawing", "tool": "pen"}
        )

    async def test_execute_prompt_for_student_action(self, conductor_services):
        """Test that the Conductor correctly handles 'prompt_for_student_action'."""
        agent, rpc_service, mock_ctx = conductor_services
        
        # Mock the TTS engine
        agent.agent_session = MagicMock()
        agent.agent_session.say = AsyncMock()
        
        # Verify initial state
        assert agent._expected_user_input_type == "INTERRUPTION"
        
        # Create a toolbelt with a prompt action
        toolbelt = [
            {"tool_name": "prompt_for_student_action", "parameters": {"prompt_text": "Please solve this equation"}},
            {"tool_name": "speak", "parameters": {"text": "This should not be executed"}}  # Should be skipped
        ]
        
        # Execute the toolbelt
        await agent._execute_toolbelt(toolbelt)
        
        # Verify the TTS was called for the prompt
        agent.agent_session.say.assert_called_once_with(text="Please solve this equation", allow_interruptions=True)
        
        # Verify the state changed to SUBMISSION
        assert agent._expected_user_input_type == "SUBMISSION"

    async def test_execute_complex_toolbelt(self, conductor_services):
        """Test executing a complex toolbelt with multiple action types."""
        agent, rpc_service, mock_ctx = conductor_services
        
        agent.agent_session = MagicMock()
        agent.agent_session.say = AsyncMock()
        
        # Mock the agent's frontend client instance methods directly
        mock_highlight = AsyncMock()
        mock_set_ui_state = AsyncMock()
        mock_show_feedback = AsyncMock()
        agent._frontend_client.highlight_element = mock_highlight
        agent._frontend_client.set_ui_state = mock_set_ui_state
        agent._frontend_client.show_feedback = mock_show_feedback
        
        # Create a complex toolbelt using correct toolbelt action names
        toolbelt = [
            {"tool_name": "speak", "parameters": {"text": "Let me highlight this for you"}},
            {"tool_name": "highlight_element", "parameters": {"element_id": "equation_1"}},
            {"tool_name": "set_ui_state", "parameters": {"mode": "explanation"}},
            {"tool_name": "show_feedback", "parameters": {"feedback_type": "info", "message": "Good job!"}}
        ]
        
        # Execute the toolbelt
        await agent._execute_toolbelt(toolbelt)
        
        # Verify all actions were executed in order
        agent.agent_session.say.assert_called_once_with(text="Let me highlight this for you", allow_interruptions=True)
        mock_highlight.assert_called_once_with(agent._room, agent.caller_identity, "equation_1")
        mock_set_ui_state.assert_called_once_with(agent._room, agent.caller_identity, {"mode": "explanation"})
        mock_show_feedback.assert_called_once_with(agent._room, agent.caller_identity, "info", "Good job!", 3000)

class TestConductorIntegration:
    """End-to-end integration tests for the complete Conductor flow."""

    async def test_full_interruption_flow(self, conductor_services):
        """Test the complete flow from RPC call to Brain to execution."""
        agent, rpc_service, mock_ctx = conductor_services
        
        # Mock all external services
        mock_brain_response = [
            {"tool_name": "speak", "parameters": {"text": "I see you have a question"}},
            {"tool_name": "set_ui_state", "parameters": {"listening": True}}
        ]
        
        agent.agent_session = MagicMock()
        agent.agent_session.say = AsyncMock()
        agent.agent_session.interrupt = MagicMock()
        
        # Mock the LangGraph client properly - ensure it returns a valid toolbelt
        mock_brain_response = [
            {"tool_name": "speak", "parameters": {"text": "I see you have a question"}},
            {"tool_name": "set_ui_state", "parameters": {"listening": True}}
        ]
        agent._langgraph_client.invoke_langgraph_task = AsyncMock(return_value=mock_brain_response)
        
        # Mock the agent's frontend client instance directly
        mock_set_ui_state = AsyncMock()
        agent._frontend_client.set_ui_state = mock_set_ui_state
        
        # 1. Simulate RPC call
        mock_rpc_payload = MagicMock()
        mock_rpc_payload.caller_identity = "test_client_123"
        
        await rpc_service.student_wants_to_interrupt(mock_rpc_payload)
        
        # 2. Process the queued task (simulate processing loop)
        task = await agent._processing_queue.get()
        toolbelt = await agent._langgraph_client.invoke_langgraph_task(
            task=task,
            user_id="test_user",
            session_id="test_session"
        )
        await agent._execute_toolbelt(toolbelt)
        
        # 3. Verify the complete flow
        # RPC handler interrupted the session
        agent.agent_session.interrupt.assert_called_once()
        
        # Brain was called with correct task
        agent._langgraph_client.invoke_langgraph_task.assert_called_once()
        call_args = agent._langgraph_client.invoke_langgraph_task.call_args[1]
        assert call_args['task'] == task
        assert call_args['user_id'] == 'test_user'
        assert call_args['session_id'] == 'test_session'
        
        # Actions were executed
        agent.agent_session.say.assert_called_once_with(text="I see you have a question", allow_interruptions=True)
        mock_set_ui_state.assert_called_once_with(agent._room, agent.caller_identity, {"listening": True})

    async def test_ping_connectivity(self, conductor_services):
        """Test the basic connectivity ping functionality."""
        agent, rpc_service, mock_ctx = conductor_services
        
        mock_rpc_payload = MagicMock()
        response = await rpc_service.TestPing(mock_rpc_payload)
        
        # Verify the response
        response_pb = interaction_pb2.AgentResponse()
        response_pb.ParseFromString(base64.b64decode(response))
        assert "Conductor" in response_pb.status_message
        assert "alive" in response_pb.status_message.lower()

if __name__ == "__main__":
    # Run the tests
    pytest.main([__file__, "-v"])
