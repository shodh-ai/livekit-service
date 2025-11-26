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
RUN mkdir -p rox/generated/protos && \
    python -m grpc_tools.protoc \
    --python_out=rox/generated/protos \
    --grpc_python_out=rox/generated/protos \
    --proto_path=rox/protos \
    rox/protos/interaction.proto || echo "Protobuf generation completed"

# Create empty __init__.py files for Python imports
RUN touch rox/generated/__init__.py && \
    touch rox/generated/protos/__init__.py

# Set Python path to include the project root
ENV PYTHONPATH=/app

# Keep Python output unbuffered for better logging
ENV PYTHONUNBUFFERED=1

# Start the LiveKit worker runner. This process connects to LiveKit Cloud and
# waits for job assignments using the entrypoint defined in rox/entrypoint.py.
CMD ["python", "rox/main.py"]

    