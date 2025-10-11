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

# Optionally pre-download any required runtime assets referenced by rox/main.py.
# Docs suggest running `python main.py download-files` inside rox/.
# Make this step non-fatal if the command is not implemented.
RUN cd rox && python main.py download-files || echo "[info] download-files not available; skipping pre-download"

# Expose the port that Cloud Run expects
EXPOSE 8080

# Set default environment variables for Cloud Run
ENV PORT=8080
ENV PYTHONUNBUFFERED=1

# Health check for Cloud Run
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8080/ || exit 1

# Start the FastAPI HTTP server; agents are launched via the /run-agent endpoint
CMD ["python", "rox/main.py", "--server"]

    