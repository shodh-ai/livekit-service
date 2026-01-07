import logging

from livekit.agents import WorkerOptions, cli

from rox.entrypoint import entrypoint
from rox.otel_setup import init_tracing


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("rox")


if __name__ == "__main__":
    # Initialize OpenTelemetry (optional; keep if used)
    try:
        init_tracing()
    except Exception:
        pass

    # Start the Worker using the LiveKit CLI, which connects to LiveKit Cloud
    # and waits for job assignments.
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="rox-agent",
            # Handle 4 students per pod to optimize cost
        )
    )
