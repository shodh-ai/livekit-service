import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from rox.agent import RoxAgent
from rox.entrypoint import _populate_agent_state_from_metadata, settings


@pytest.mark.asyncio
async def test_transcript_interceptor_flushes_correctly():
    agent = RoxAgent()
    interceptor = agent.llm_interceptor
    agent.trigger_langgraph_task = AsyncMock()

    interceptor._buffer.append("Hello there.")
    interceptor._buffer.append("This is a test.")

    await interceptor._flush()

    assert agent.trigger_langgraph_task.await_count == 1
    _, kwargs = agent.trigger_langgraph_task.call_args
    assert kwargs["task_name"] == "student_spoke_or_acted"
    payload = json.loads(kwargs["json_payload"]) if isinstance(kwargs["json_payload"], str) else kwargs["json_payload"]
    assert payload["transcript"] == "Hello there. This is a test."


@pytest.mark.asyncio
async def test_populate_agent_state_from_metadata_precedence_env_over_room_over_participant():
    agent = RoxAgent()

    # Backup and set ENV settings override
    orig_meta = getattr(settings, "STUDENT_TOKEN_METADATA", "")
    try:
        settings.STUDENT_TOKEN_METADATA = json.dumps({
            "user_id": "env-user",
            "curriculum_id": "env-curr",
            "current_lo_id": "env-lo"
        })

        # Mock ctx with empty room metadata (should be ignored due to env override)
        ctx = MagicMock()
        ctx.room = MagicMock()
        ctx.room.metadata = None
        ctx.room.remote_participants = {}

        await _populate_agent_state_from_metadata(agent, ctx)
        assert agent.user_id == "env-user"
        assert agent.curriculum_id == "env-curr"
        assert agent.current_lo_id == "env-lo"

        # Now clear env and use room metadata
        settings.STUDENT_TOKEN_METADATA = ""
        room_meta = json.dumps({
            "user_id": "room-user",
            "curriculum_id": "room-curr",
            "current_lo_id": "room-lo"
        })
        async def room_meta_coro():
            return room_meta
        ctx.room.metadata = room_meta_coro()

        await _populate_agent_state_from_metadata(agent, ctx)
        assert agent.user_id == "room-user"
        assert agent.curriculum_id == "room-curr"
        assert agent.current_lo_id == "room-lo"

        # Finally clear room and use participant metadata
        ctx.room.metadata = None
        participant = MagicMock()
        participant.identity = "student-1"
        async def participant_meta_coro():
            return json.dumps({
                "user_id": "p-user",
                "curriculum_id": "p-curr",
                "current_lo_id": "p-lo"
            })
        participant.metadata = participant_meta_coro()
        ctx.room.remote_participants = {"p1": participant}

        await _populate_agent_state_from_metadata(agent, ctx)
        assert agent.user_id == "p-user"
        assert agent.curriculum_id == "p-curr"
        assert agent.current_lo_id == "p-lo"
    finally:
        settings.STUDENT_TOKEN_METADATA = orig_meta
