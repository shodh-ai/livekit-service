# Use Python 3.11 slim as base image for better performance and security
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies required for audio processing and gRPC
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    make \
    pkg-config \
    libffi-dev \
    libssl-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better Docker layer caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy the entire project
COPY . .

# Generate protobuf files if they don't exist
RUN mkdir -p generated/protos && \
    python -m grpc_tools.protoc \
    --python_out=generated/protos \
    --grpc_python_out=generated/protos \
    --proto_path=. \
    interaction.proto || echo "Protobuf generation completed"

# Create empty __init__.py files for Python imports
RUN touch generated/__init__.py && \
    touch generated/protos/__init__.py

# Set Python path to include the project root
ENV PYTHONPATH=/app

# Expose the port that Cloud Run expects
EXPOSE 8080

# Set default environment variables for Cloud Run
ENV PORT=8080
ENV PYTHONUNBUFFERED=1

# Health check for Cloud Run
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8080/ || exit 1

# CHANGE: Check if we should run as agent or HTTP server
# If LIVEKIT_URL is set, run as agent; otherwise run HTTP server
CMD if [ -n "$LIVEKIT_URL" ] && [ -n "$ROOM_NAME" ]; then \
      echo "Starting as LiveKit Agent for room: $ROOM_NAME"; \
      python rox/main.py connect --url "$LIVEKIT_URL" --room "$ROOM_NAME" --api-key "$LIVEKIT_API_KEY" --api-secret "$LIVEKIT_API_SECRET"; \
    else \
      echo "Starting as HTTP Server"; \
      python -m uvicorn rox.main:app --host 0.0.0.0 --port 8080; \
    fi

    