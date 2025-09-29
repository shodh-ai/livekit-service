# File: mock_brain.py
import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import random

# This mimics the request body your livekit-service will send
class BrainRequest(BaseModel):
    session_id: str
    student_id: str
    curriculum_id: str
    student_input: Optional[str] = None
    restored_feed_summary: Optional[Dict[str, Any]] = None
    block_id: Optional[str] = None
    block_content: Optional[Dict[str, Any]] = None

app = FastAPI()

@app.post("/start_session")
async def mock_start_session(request: BrainRequest):
    print("\n--- ✅ MOCK BRAIN: Received /start_session ---")
    print(f"Session ID: {request.session_id}")
    if request.restored_feed_summary:
        try:
            keys = list((request.restored_feed_summary or {}).keys())
            print("Restored Feed Summary: SUCCESS! Received summary with keys:", keys)
        except Exception:
            print("Restored Feed Summary: Present (could not list keys)")
    else:
        print("Restored Feed Summary: Not provided (this is expected for a fresh session).")
    print("-------------------------------------------------")

    # Send back a simple welcome plan
    return {
        "delivery_plan": {
            "actions": [
                {"tool_name": "speak", "parameters": {"text": "Hello from the Mock Brain!"}},
                {"tool_name": "listen", "parameters": {}}
            ]
        }
    }

@app.post("/handle_response")
async def mock_handle_response(request: BrainRequest):
    print("\n--- ✅ MOCK BRAIN: Received /handle_response ---")
    print(f"Session ID: {request.session_id}")
    print(f"Student Input: {request.student_input}")

    # This is where you can test the get_block_content response
    if request.block_content:
        print("Block Content: SUCCESS! Received content for block ID:", request.block_id)
        print("Block Summary:", (request.block_content or {}).get("summary"))
        # Now, pretend to use this content to generate a new command
        return {
            "delivery_plan": {
                "actions": [
                    {"tool_name": "speak", "parameters": {"text": f"I have received the content for {(request.block_content or {}).get('summary')}. Let's update it."}},
                    {
                        "tool_name": "update_excalidraw_block",
                        "parameters": {
                            "block_id": request.block_id,
                            "modifications": '[{"type": "text", "text": "Updated by the AI!"}]'
                        }
                    },
                    {"tool_name": "listen", "parameters": {}}
                ]
            }
        }

    # If no block content, decide which tool to test based on student input
    if "test get content" in (request.student_input or "").lower():
        # Tell the Conductor to fetch content for 'block-X'
        delivery_plan = {
            "actions": [
                {"tool_name": "speak", "parameters": {"text": "Okay, I will now fetch the content for block-X."}},
                {"tool_name": "get_block_content", "parameters": {"block_id": "block-X"}}
            ]
        }
    else:
        # Default action: just add a new block
        delivery_plan = {
            "actions": [
                {"tool_name": "speak", "parameters": {"text": f"You said: {request.student_input}. I will now add a new block."}},
                {
                    "tool_name": "add_excalidraw_block",
                    "parameters": {
                        "block_id": f"block-from-ai-{random.randint(100,999)}",
                        "summary": "AI Generated Block",
                        "initial_elements": '[{"type": "ellipse"}]'
                    }
                },
                {"tool_name": "listen", "parameters": {}}
            ]
        }

    return {"delivery_plan": delivery_plan}

if __name__ == "__main__":
    print("🚀 Starting Mock Brain server on http://localhost:8001")
    uvicorn.run(app, host="0.0.0.0", port=8001)
