#!/usr/bin/env python3
"""
Clean launcher for Rox Agent service.
This file intentionally contains no class or app definitions.
"""

import os
import sys
import logging

# Ensure parent directory (project root) is on sys.path when executed as a script
if __package__ in (None, ""):
    sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from livekit.agents import WorkerOptions, cli

# Delegated modules (absolute imports)
from rox.server import run_fastapi_server
from rox.entrypoint import entrypoint
from rox.otel_setup import init_tracing

# Basic logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def main():
    """Determines whether to run the FastAPI server or the LiveKit agent."""
    is_server = len(sys.argv) == 1 or (len(sys.argv) > 1 and sys.argv[1] == "--server")
    # Initialize OpenTelemetry: disable log export in server mode to avoid force_flush timeouts
    try:
        init_tracing(enable_logs=not is_server)
    except Exception:
        logger.debug("OTel init failed (non-fatal)", exc_info=True)
    if is_server:
        logger.info("Starting in FastAPI server mode.")
        run_fastapi_server()
    else:
        logger.info("Starting in LiveKit Agent mode.")
        try:
            cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
        except Exception as e:
            logger.error(f"Failed to start LiveKit Agent CLI: {e}", exc_info=True)


if __name__ == "__main__":
    main()
