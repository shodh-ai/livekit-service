# livekit-service/mock_kamikaze.py
# A mock FastAPI server that impersonates the Kamikaze/LangGraph "Brain".
# It receives requests from the RoxAgent Conductor and replies with
# pre-defined, fixed toolbelts for testing purposes.

import logging
from typing import Dict, Any, Optional, List

from fastapi import FastAPI, Request
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Mock Kamikaze/LangGraph Brain")

# --- Pre-defined Toolbelts for Testing ---

TOOLBELT_BROWSER_BASICS: Dict[str, Any] = {
    "actions": [
        {"tool_name": "speak", "parameters": {"text": "Running the Browser Basics test toolbelt."}},
        {"tool_name": "browser_navigate", "parameters": {"url": "https://www.google.com/search?q=livekit"}},
        {"tool_name": "wait", "parameters": {"ms": 2000}},
        {"tool_name": "browser_type", "parameters": {"selector": "textarea[name=q]", "text": " agents framework"}},
        {"tool_name": "wait", "parameters": {"ms": 1000}},
        {"tool_name": "browser_key_press", "parameters": {"key": "Enter"}},
        {"tool_name": "speak", "parameters": {"text": "Browser test complete."}},
        {"tool_name": "listen", "parameters": {}},
    ]
}

TOOLBELT_VSCODE_TEST: Dict[str, Any] = {
    "actions": [
        {"tool_name": "speak", "parameters": {"text": "Now running the VS Code Integration test toolbelt."}},
        {"tool_name": "browser_navigate", "parameters": {"url": "http://localhost:4600"}},
        {"tool_name": "wait", "parameters": {"ms": 5000}},
        {"tool_name": "speak", "parameters": {"text": "I will now create a test file and add some content."}},
        {"tool_name": "vscode_add_line", "parameters": {"path": "test_from_mock.txt", "lineNumber": 1, "content": "# Hello from the Mock Kamikaze Brain!"}},
        {"tool_name": "wait", "parameters": {"ms": 2000}},
        {"tool_name": "vscode_run_terminal_command", "parameters": {"command": "echo 'Mock terminal test successful!'"}},
        {"tool_name": "speak", "parameters": {"text": "VS Code test complete."}},
        {"tool_name": "listen", "parameters": {}},
    ]
}

# A simple way to cycle through the test toolbelts
TEST_CYCLE: List[Dict[str, Any]] = [TOOLBELT_BROWSER_BASICS, TOOLBELT_VSCODE_TEST]
current_test_index: int = 0

# --- Pydantic models to match the real API contract ---
class SessionRequest(BaseModel):
    session_id: str
    student_id: str
    curriculum_id: str
    student_input: Optional[str] = None
    visual_context: Optional[Dict[str, Any]] = None
    # Additional passthrough fields are allowed by default in pydantic v2

class SessionResponse(BaseModel):
    delivery_plan: Dict[str, Any]


def _next_toolbelt() -> Dict[str, Any]:
    global current_test_index
    toolbelt_to_send = TEST_CYCLE[current_test_index]
    current_test_index = (current_test_index + 1) % len(TEST_CYCLE)
    return toolbelt_to_send


@app.post("/handle_response", response_model=SessionResponse)
@app.post("/handle_interruption", response_model=SessionResponse)
@app.post("/start_session", response_model=SessionResponse)
async def handle_any_request(payload: SessionRequest, request: Request):
    """Catch all endpoints and cycle through test toolbelts."""
    try:
        path = str(request.url.path)
    except Exception:
        path = "(unknown)"

    # Log the incoming request to verify the Conductor is calling us correctly
    try:
        vc = payload.visual_context or {}
        has_b64 = bool(vc.get("screenshot_b64"))
        logger.info(
            "Received request on %s | session_id=%s student_id=%s input=%r screenshot=%s",
            path,
            payload.session_id,
            payload.student_id,
            (payload.student_input or ""),
            has_b64,
        )
    except Exception:
        logger.exception("Failed to log incoming request")

    toolbelt_to_send = _next_toolbelt()
    logger.info("Responding with toolbelt #%s (actions=%d)", current_test_index or len(TEST_CYCLE), len(toolbelt_to_send.get("actions", [])))

    return SessionResponse(delivery_plan=toolbelt_to_send)


@app.get("/")
async def health_check():
    return {"status": "ok", "message": "Mock Kamikaze Brain is running"}


# To run this server locally:
# 1. pip install -r requirements.txt
# 2. uvicorn mock_kamikaze:app --host 0.0.0.0 --port 8001
