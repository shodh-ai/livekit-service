# Use Python 3.11 slim image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies for audio/video processing and build tools
RUN apt-get update && apt-get install -y \
    ffmpeg \
    build-essential \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first to leverage Docker cache
COPY requirements.txt .

# Install Python dependencies (with caching for faster builds)
# Install build dependencies like Cython and wheel first
RUN pip install Cython wheel

# Install Python dependencies (with caching for faster builds)
RUN pip install -r requirements.txt

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