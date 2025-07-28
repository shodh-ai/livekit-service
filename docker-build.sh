#!/bin/bash

# Build the Docker image
echo "Building Rox Agent Docker image..."
docker build -t rox-agent .

echo "Build complete!"
echo ""
echo "Usage examples:"
echo "  # Run as FastAPI server:"
echo "  docker run -p 5005:5005 --env-file .env rox-agent"
echo ""
echo "  # Run as LiveKit agent:"
echo "  docker run --env-file .env rox-agent python main.py connect --room my-room --url wss://..."
echo ""
