# custom_llm.py
"""Custom LLM bridge for Rox that forwards each user transcript to a single
FastAPI streaming endpoint (`POST /invoke_task_streaming`) and streams
assistant text responses back to the LiveKit agent framework.

The backend is expected to:
1. Accept a JSON body: `{"transcript": str, "user_token": str, "user_id": str}`
2. Respond as a Server-Sent Events (SSE) stream where **each event** contains a
   JSON object with a `content` field holding the assistant’s partial / final
   text.
No TTS pushing or additional LangGraph calls are performed – this file is a
minimal uni-directional bridge.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

import httpx
from livekit.agents.llm import (
    LLM,
    ChatChunk,
    ChatContext,
    ChoiceDelta,
)

logger = logging.getLogger(__name__)

STREAMING_URL_DEFAULT = os.getenv(
    "MY_CUSTOM_AGENT_URL", "http://localhost:8080/invoke_task_streaming"
)


class CustomLLMBridge(LLM):
    """Minimal LLM wrapper that pipes transcript → FastAPI → SSE → ChatChunk."""

    def __init__(self, streaming_url: str = STREAMING_URL_DEFAULT):
        super().__init__()
        self._streaming_url = streaming_url
        self._user_token: str = os.getenv("DEFAULT_USER_TOKEN", "")
        self._user_id: str = os.getenv("DEFAULT_USER_ID", "anonymous_user")
        logger.info("CustomLLMBridge initialised – streaming endpoint: %s", self._streaming_url)

    # ---------------------------------------------------------------------
    # Public helpers
    # ---------------------------------------------------------------------
    def add_user_token(self, user_token: str, user_id: str) -> None:  # noqa: D401
        """Store auth token / user id to include in future requests."""
        self._user_token = user_token
        self._user_id = user_id
        logger.debug("User token updated (user_id=%s).", user_id)

    # ------------------------------------------------------------------
    # livekit.agents.llm.LLM override
    # ------------------------------------------------------------------
    def chat(self, *, chat_ctx: ChatContext = None, tools=None, tool_choice=None):  # noqa: D401
        return self._chat_context_manager(chat_ctx)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @asynccontextmanager
    async def _chat_context_manager(self, chat_ctx: ChatContext):  # noqa: D401
        """Async context manager yielding a stream of ChatChunk objects."""

        async def _stream_chunks() -> AsyncIterator[ChatChunk]:
            transcript = _extract_latest_user_transcript(chat_ctx)
            if not transcript:
                logger.warning("No user transcript found in ChatContext – yielding empty chunk.")
                yield _empty_chunk()
                return

            inner_payload = {
                "transcript": transcript,
                "user_token": self._user_token,
                "user_id": self._user_id,
            }
            request_body = {
                "task_name": "speaking_turn",
                "json_payload": json.dumps(inner_payload),
            }
            logger.info(
                "Posting speaking_turn (len=%d) to %s", len(transcript), self._streaming_url
            )

            try:
                async with httpx.AsyncClient(timeout=120.0) as client:
                    async with client.stream("POST", self._streaming_url, json=request_body) as resp:
                        resp.raise_for_status()
                        async for chunk in _sse_to_chat_chunks(resp):
                            yield chunk
            except httpx.HTTPError as exc:
                logger.error("HTTP error from streaming backend: %s", exc, exc_info=True)
                yield _error_chunk("Backend HTTP error; see logs.")
            except Exception as exc:  # noqa: BLE001
                logger.error("Unexpected error in CustomLLMBridge: %s", exc, exc_info=True)
                yield _error_chunk("Unexpected backend error; see logs.")

        try:
            yield _stream_chunks()
        finally:
            pass  # nothing to clean up


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _extract_latest_user_transcript(chat_ctx: Optional[ChatContext]) -> str:
    """Return the content of the last *user* message in the ChatContext."""
    if not chat_ctx:
        return ""

    # ChatContext stores messages in a private `_items` list in newest-last order.
    messages = getattr(chat_ctx, "_items", [])
    for msg in reversed(messages):  # iterate from newest to oldest
        try:
            role = getattr(msg, "role", None) or (msg.get("role") if isinstance(msg, dict) else None)
            if str(role).lower() == "user":
                content = getattr(msg, "content", None) or (
                    msg.get("content") if isinstance(msg, dict) else None
                )
                if isinstance(content, list):
                    return " ".join(map(str, content))
                return str(content)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Error while scanning ChatContext message: %s", exc)
    return ""


def _empty_chunk() -> ChatChunk:
    return ChatChunk(id=str(uuid.uuid4()), delta=ChoiceDelta(role="assistant", content=""))


def _error_chunk(msg: str) -> ChatChunk:
    return ChatChunk(id=str(uuid.uuid4()), delta=ChoiceDelta(role="assistant", content=msg))


async def _sse_to_chat_chunks(resp: httpx.Response) -> AsyncIterator[ChatChunk]:
    """Convert an SSE stream from backend into ChatChunk objects."""
    event_data_lines: list[str] = []
    async for line_bytes in resp.aiter_bytes():
        line = line_bytes.decode().strip()
        if line.startswith("data:"):
            event_data_lines.append(line[len("data:") :].strip())
        elif line == "":  # empty line signals end of event
            if not event_data_lines:
                continue
            data_str = "\n".join(event_data_lines)
            event_data_lines.clear()
            try:
                data_obj = json.loads(data_str)
                content = data_obj.get("content", "") if isinstance(data_obj, dict) else str(data_obj)
            except json.JSONDecodeError:
                content = data_str  # treat raw text
            yield ChatChunk(id=str(uuid.uuid4()), delta=ChoiceDelta(role="assistant", content=content))
        # Ignore other SSE fields (event:, id:, retry:)




