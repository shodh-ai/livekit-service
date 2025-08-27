# livekit-service/Dockerfile
# Use Python 3.11 slim image
# ---- Build Stage ----
FROM python:3.11-slim AS builder

# Set working directory
WORKDIR /app

# Install build-time system dependencies only
RUN apt-get update && apt-get install -y \
    build-essential \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python deps with build tools
COPY requirements.txt .
RUN pip install --no-cache-dir Cython wheel \
 && pip install --no-cache-dir -r requirements.txt

# Copy the application code
COPY . .

# Regenerate protobuf files to match the installed grpcio-tools version
# This ensures the generated code is compatible with the libraries in the container
RUN python -m grpc_tools.protoc \
    -I/app/rox/protos \
    --python_out=/app/rox/generated/protos \
    --pyi_out=/app/rox/generated/protos \
    --grpc_python_out=/app/rox/generated/protos \
    /app/rox/protos/interaction.proto

# Download the required models for the livekit agents
RUN python rox/main.py download-files

# ---- Final Stage ----
FROM python:3.11-slim

# Create a non-root user and group
RUN groupadd -r appuser && useradd -r -g appuser appuser

# Set working directory
WORKDIR /app

# Install runtime-only dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages and application from the builder stage
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /app /app

# Set ownership and drop privileges
RUN chown -R appuser:appuser /app
USER appuser

# Set working directory to rox for the unified service
WORKDIR /app/rox

# Expose the service port
EXPOSE 5005

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app:/app/rox

# Default command runs the FastAPI server
# To run as LiveKit agent, override with: docker run <image> python main.py connect --room <room> --url <url> ...
CMD ["python", "main.py", "--server"]