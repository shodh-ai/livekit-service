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
    # === FIX: Install uvloop for better async performance ===
    # uvloop provides 2-4x faster event loop than default asyncio
    # Critical for handling 10k+ concurrent users
    try:
        import uvloop
        uvloop.install()
        logger.info("[UVLOOP] ✅ uvloop installed (2-4x better async performance)")
    except ImportError:
        logger.warning("[UVLOOP] ⚠️  uvloop not available, using default asyncio event loop (slower)")
    except Exception as e:
        logger.warning(f"[UVLOOP] ⚠️  Failed to install uvloop: {e}")
    # === END FIX ===

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
            # Handle 4 students per pod to optimize cost
        )
    )
