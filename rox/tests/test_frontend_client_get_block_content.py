# Tests: FrontendClient.get_block_content_from_frontend request/response parsing

import base64
import json
from pathlib import Path
import sys
import asyncio

import pytest

THIS_DIR = Path(__file__).resolve().parent
ROX_DIR = THIS_DIR.parent
if str(ROX_DIR) not in sys.path:
    sys.path.insert(0, str(ROX_DIR))

from frontend_client import FrontendClient  # noqa: E402
from generated.protos import interaction_pb2  # noqa: E402


class FakeLocalParticipant:
    def __init__(self, response_json: dict, success: bool = True):
        self._response_json = response_json
        self._success = success

    async def perform_rpc(self, destination_identity: str, method: str, payload: str):
        # Ignore inputs and return a serialized ClientUIActionResponse with message set to JSON
        resp = interaction_pb2.ClientUIActionResponse(
            request_id="test-req",
            success=self._success,
            message=json.dumps(self._response_json) if self._response_json is not None else "",
        )
        return base64.b64encode(resp.SerializeToString()).decode("utf-8")


class FakeRoom:
    def __init__(self, response_json: dict, success: bool = True):
        self.local_participant = FakeLocalParticipant(response_json, success)


@pytest.mark.asyncio
async def test_get_block_content_from_frontend_success_json_parsed():
    client = FrontendClient()
    room = FakeRoom({"id": "block-1", "type": "excalidraw", "elements": []}, success=True)
    data = await client.get_block_content_from_frontend(room, identity="student-1", block_id="block-1", timeout_sec=2.0)
    assert isinstance(data, dict)
    assert data["id"] == "block-1"


@pytest.mark.asyncio
async def test_get_block_content_from_frontend_failure_returns_none():
    client = FrontendClient()
    # success False should produce None
    room = FakeRoom({"id": "block-1"}, success=False)
    data = await client.get_block_content_from_frontend(room, identity="student-1", block_id="block-1", timeout_sec=2.0)
    assert data is None
