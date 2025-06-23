import subprocess
import sys
from fastapi import FastAPI, HTTPException
from model import AgentRequest
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

import os
app = FastAPI()


@app.get("/")
def read_root():
    return {"Hello": "World"}


@app.post("/run-agent")
def run_agent(agent_request: AgentRequest):
    room_name = agent_request.room_name
    room_url = agent_request.room_url
    api_key = os.getenv("LIVEKIT_API_KEY")
    api_secret = os.getenv("LIVEKIT_API_SECRET")

    if not all([api_key, api_secret]):
        raise HTTPException(
            status_code=500,
            detail="Server configuration error: LIVEKIT_API_KEY and LIVEKIT_API_SECRET must be set."
        )
    rox_main_path = "rox/main.py"
    python_executable = sys.executable  # Use the same python interpreter
    print(f"Starting agent for room: {room_name}")
    print(f"Room url: {room_url}")

    try:
        # +++ CORRECTED COMMAND CONSTRUCTION +++
        # This matches the format expected by the livekit.agents.cli
        command = [
            python_executable,
            rox_main_path,
            "connect",          # The command for the CLI
            "--url",           # The URL flag
            room_url,           # The URL value
            "--room",           # The room flag
            room_name,
            "--api-key",        # The API key flag
            api_key,
            "--api-secret",     # The API secret flag
            api_secret,
        ]
        print(f"Executing command: {' '.join(command)}")

        # Run the rox script as a non-blocking subprocess
        process = subprocess.Popen(command)

        # Optionally, you might want to store the process ID or manage it
        # For now, we'll just return a success message indicating it started
        return {"message": f"Agent started for room {room_name}", "pid": process.pid}
    except FileNotFoundError:
        raise HTTPException(
            status_code=500, detail=f"Error: {rox_main_path} not found."
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to run agent: {str(e)}")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=5005)