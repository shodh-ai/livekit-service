# Use Python 3.11 slim image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy requirements first to leverage Docker cache
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Install dependencies for rox
COPY rox/requirements.txt rox/requirements.txt
RUN pip install --no-cache-dir -r rox/requirements.txt

# Copy the application code
COPY . .

# Expose the service port (adjust based on your configuration)
EXPOSE 5005
# Set environment variables
ENV PYTHONUNBUFFERED=1

# Start the application
CMD ["python", "main.py"]