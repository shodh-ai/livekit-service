# livekit-service/PYTHON_VERSION.md
# Python Environment Setup for LiveKit Conductor Service

## Required Python Version
- **Python 3.9.21** (tested and validated)
- Compatible with Python 3.9.x series

## Virtual Environment Setup

### 1. Create Virtual Environment
```bash
python3.9 -m venv venv
```

### 2. Activate Virtual Environment
```bash
source venv/bin/activate
```

### 3. Upgrade pip (recommended)
```bash
pip install --upgrade pip
```

### 4. Install Dependencies
```bash
pip install -r requirements.txt
```

## Critical Version Notes

### LiveKit Dependencies
- **LiveKit SDK 1.0.6** - Core real-time communication framework
- **LiveKit Agents 1.0.6** - Voice agent framework with Deepgram, Silero, and turn detection
- **LiveKit Protocol 1.0.2** - Protocol definitions for LiveKit communication

### Protobuf Version
- **Must use Protobuf >= 6.31.0** to match generated protobuf code
- This is critical for RPC communication with frontend and Brain service
- Generated protobuf files are compatible with version 6.x

### FastAPI and Web Framework
- **FastAPI 0.104.1** - Web framework for HTTP API endpoints
- **Uvicorn 0.23.2** - ASGI server for running the FastAPI application

### Testing Framework
- **pytest 7.4.0+** - Testing framework
- **pytest-asyncio 0.21.0+** - Async testing support
- **pytest-mock 3.11.0+** - Mocking support for integration tests

## Environment Variables
Ensure your `.env` file contains:
```bash
LIVEKIT_URL=your_livekit_url
LIVEKIT_API_KEY=your_api_key
LIVEKIT_API_SECRET=your_api_secret
DEEPGRAM_API_KEY=your_deepgram_key
OPENAI_API_KEY=your_openai_key
```

## Verification Commands

After installation, verify the setup:
```bash
# Test imports
python -c "import livekit; import fastapi; import dotenv; print('✅ Core dependencies loaded')"

# Run integration tests
pytest tests/test_conductor.py -v

# Test Brain communication
python test_conductor_brain_link.py
```

## Troubleshooting

### Protobuf Version Mismatch
If you get protobuf version errors:
```bash
pip install --upgrade protobuf
```

### LiveKit Connection Issues
Ensure your `.env` file has correct LiveKit credentials and the LiveKit server is accessible.

### Audio Dependencies
On macOS, you may need to install additional audio libraries:
```bash
brew install portaudio
```

### RPC Communication Issues
Ensure the generated protobuf files are up to date:
```bash
# Regenerate if needed (from pronity-frontend/protos/interaction.proto)
python -m grpc_tools.protoc --python_out=. --grpc_python_out=. interaction.proto
```
