# LiveKit Voice Agent + Weather Backend

This project provides a full-stack voice AI agent pipeline using [LiveKit](https://livekit.io/), Deepgram, and OpenAI. It includes both a real-time voice agent (with advanced echo cancellation) and an external backend agent that can answer weather queries using OpenAI function calling.

## Features
- Real-time voice-to-voice conversation via LiveKit
- Custom LLM bridge to external backend (Flask/OpenAI)
- Weather agent backend with OpenAI function calling and mock weather data
- Deepgram-powered speech-to-text and text-to-speech
- Robust error handling and logging
- .env-based configuration for API keys and service URLs

## Directory Structure
- `vpa_new/` — Main LiveKit agent, custom LLM bridge, and backend integration
- `vpa_new/external_agent.py` — Flask backend for weather queries (uses OpenAI)
- `requirements.txt` — All Python dependencies

## Installation

### 1. Clone the repository
```sh
git clone <your-repo-url>
cd livekit-agent-server
```

### 2. Set up Python environment
It is recommended to use a virtual environment:
```sh
python3 -m venv venv
source venv/bin/activate
```

### 3. Install dependencies
```sh
pip install -r requirements.txt
```

### 4. Environment Variables
Create a `.env` file in the `vpa_new/` directory. You will need to set your API keys and configuration values. Example variables:
```
DEEPGRAM_API_KEY=<Your Deepgram API Key>
OPENAI_API_KEY=<Your OpenAI API Key>
LIVEKIT_API_KEY=<your API Key>
LIVEKIT_API_SECRET=<your API Secret>
LIVEKIT_URL=<your LiveKit server URL>
MY_CUSTOM_AGENT_URL=http://localhost:5005/process
```

**Do not commit your .env file or API keys to source control.**

## Usage

### Start the Weather Backend
In one terminal:
```sh
cd vpa_new
python3 external_agent.py

```

### Start the LiveKit Voice Agent
In another terminal:
```sh
cd vpa_new
python3 main_copy.py connect --room <room-name> --page-path <page-path>
#for speaking room 
python3 main.py_copy connect --room Speakingpage --page-path speakingpage
#for vocabulary room 
python3 main.py_copy connect --room Vocabularypractise --page-path vocabpage
```

### for gooogle realtime

```sh
cd full_implementation
python3 main_copy.py connect --room <room-name> --page-path <page-path>
python main.py connect --room <room_name> --page-path <page_path>
#for speaking room 
python3 main.py connect --room Speakingpage --page-path speakingpage
#for vocabulary room 
python3 main.py connect --room Vocabularypractise --page-path vocabpage
```

- Speak your query (e.g., "What's the weather in Tokyo?")
- The agent will transcribe, process, and respond using the backend.

## Troubleshooting
- Ensure both the backend and agent are running and listening on the correct ports.
- Check your `.env` file for correct API keys and URLs.
- Review logs for detailed error messages.

