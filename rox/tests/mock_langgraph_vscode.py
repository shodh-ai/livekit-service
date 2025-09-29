# livekit-service/mock_langgraph_vscode.py
# A minimal FastAPI mock of the Kamikaze/LangGraph Brain that always returns
# a fixed VSCode toolbelt for faster end-to-end testing with the RoxAgent.

import logging
import os
import asyncio
from fastapi import FastAPI, Request
import uvicorn

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- DEFINE YOUR TEST TOOLBELT HERE ---
# This is the specific set of VSCode commands you want to test.

# Configurable waits
POD_JOIN_WAIT_MS = int(os.getenv("POD_JOIN_WAIT_MS", "80000"))  # wait inside toolbelt
RESPONSE_DELAY_MS = int(os.getenv("RESPONSE_DELAY_MS", "0"))    # delay HTTP response itself

WAIT_FOR_POD_ACTION = {
    "tool_name": "wait",
    "parameters": {"ms": POD_JOIN_WAIT_MS},
}

TEST_ACTIONS = [
    WAIT_FOR_POD_ACTION,
    {
        "tool_name": "speak",
        "parameters": {
            "text": "Okay, I'm going to set up the file for our exercise."
        }
    },
    {
        "tool_name": "vscode_add_line",
        "parameters": {
            "path": "test.py",
            "lineNumber": 1,
            "content": "# Hello from the local test!"
        }
    },
    {
        "tool_name": "speak",
        "parameters": {
            "text": "Now, let's run a command in the terminal."
        }
    },
    {
        "tool_name": "vscode_run_terminal_command",
        "parameters": {
            "command": "echo 'Testing terminal command execution...'"
        }
    },
    {
        "tool_name": "speak",
        "parameters": {
            "text": "Setup is complete. Does that look right on your end?"
        }
    },
    {
        "tool_name": "listen",
        "parameters": {}
    },
]

VSCODE_TEST_TOOLBELT = {
    "delivery_plan": {"actions": TEST_ACTIONS},
    "current_lo_id": "local_test_lo_1",
}
# -----------------------------------------

app = FastAPI(title="Mock LangGraph VSCode Test")

@app.post("/start_session")
async def start_session(request: Request):
    """
    Called by the RoxAgent when a session starts.
    Always returns our predefined VSCode toolbelt.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    logger.info(f"[mock_langgraph_vscode] /start_session for session_id={body.get('session_id')}")
    if RESPONSE_DELAY_MS > 0:
        logger.info(f"Delaying /start_session response by {RESPONSE_DELAY_MS} ms to allow pod join...")
        await asyncio.sleep(RESPONSE_DELAY_MS / 1000.0)
    return VSCODE_TEST_TOOLBELT

@app.post("/handle_response")
async def handle_response(request: Request):
    """
    Placeholder for follow-up turns. Responds with a minimal plan.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    logger.info(f"[mock_langgraph_vscode] /handle_response for session_id={body.get('session_id')}")
    if RESPONSE_DELAY_MS > 0:
        logger.info(f"Delaying /handle_response response by {RESPONSE_DELAY_MS} ms to allow pod join...")
        await asyncio.sleep(RESPONSE_DELAY_MS / 1000.0)
    return {
        "delivery_plan": {
            "actions": [
                {"tool_name": "speak", "parameters": {"text": "Acknowledged."}},
                {"tool_name": "listen", "parameters": {}}
            ]
        },
        "current_lo_id": VSCODE_TEST_TOOLBELT.get("current_lo_id")
    }

@app.post("/handle_interruption")
async def handle_interruption(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    logger.info(f"[mock_langgraph_vscode] /handle_interruption for session_id={body.get('session_id')}")
    if RESPONSE_DELAY_MS > 0:
        logger.info(f"Delaying /handle_interruption response by {RESPONSE_DELAY_MS} ms to allow pod join...")
        await asyncio.sleep(RESPONSE_DELAY_MS / 1000.0)
    return {
        "delivery_plan": {
            "actions": [
                {"tool_name": "speak", "parameters": {"text": "Interruption noted, continuing."}},
                {"tool_name": "listen", "parameters": {}}
            ]
        },
        "current_lo_id": VSCODE_TEST_TOOLBELT.get("current_lo_id")
    }

@app.get("/")
async def root():
    return {"status": "ok", "service": "mock_langgraph_vscode"}

if __name__ == "__main__":
    # Match the default expected by livekit-service/rox/langgraph_client.py
    uvicorn.run(app, host="0.0.0.0", port=8001)
