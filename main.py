import subprocess
import sys
from fastapi import FastAPI, HTTPException
from model import AgentRequest

app = FastAPI()


@app.get("/")
def read_root():
    return {"Hello": "World"}


@app.post("/run-agent")
def run_agent(agent_request: AgentRequest):
    room_name = agent_request.room_name
    room_url = agent_request.room_url

    rox_main_path = "rox/main.py"
    python_executable = sys.executable  # Use the same python interpreter

    print("Room name: ", room_name)
    print("Room url: ", room_url)

    try:
        # Construct the command
        command = [
            python_executable,
            rox_main_path,
            "dev",
            "--room",
            room_name,
            "--url",
            room_url,
        ]

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
