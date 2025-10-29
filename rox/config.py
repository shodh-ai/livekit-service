from __future__ import annotations

from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # General
    MIC_ENABLE_DEBOUNCE_SEC: float = 0.5
    PORT: int = 8080

    # LiveKit
    LIVEKIT_URL: Optional[str] = None
    LIVEKIT_API_KEY: Optional[str] = None
    LIVEKIT_API_SECRET: Optional[str] = None

    # Frontend RPC
    FRONTEND_RPC_TIMEOUT_SEC: float = 15.0

    # Browser pod waits
    BROWSER_JOIN_WAIT_SEC: float = 6.0
    BROWSER_JOIN_WATCH_SEC: float = 60.0

    # Session lifecycle
    STUDENT_DISCONNECT_GRACE_SEC: float = 12.0
    ROX_NO_SHOW_TIMEOUT_SECONDS: int = 60

    # LangGraph service
    LANGGRAPH_TUTOR_URL: Optional[str] = None
    LANGGRAPH_API_URL: Optional[str] = None
    MY_CUSTOM_AGENT_URL: Optional[str] = None
    LANGGRAPH_TOTAL_TIMEOUT: float = 120.0
    LANGGRAPH_CONNECT_TIMEOUT: float = 10.0
    LANGGRAPH_SOCK_CONNECT_TIMEOUT: Optional[float] = None
    LANGGRAPH_READ_TIMEOUT: float = 110.0
    LANGGRAPH_MAX_RETRIES: int = 3
    LANGGRAPH_BACKOFF_BASE: float = 1.5
    VNC_LISTENER_HTTP_URL: Optional[str] = None
    BROWSER_POD_HTTP_URL: Optional[str] = None
    LANGGRAPH_INCLUDE_VISUAL_CONTEXT: bool = True
    LANGGRAPH_ATTACH_BUFFERS: bool = True

    # Redis
    

    # Deepgram
    DEEPGRAM_API_KEY: Optional[str] = None

    # TTS selection
    TTS_PROVIDER: str = "deepgram"  # options: deepgram, gemini_native

    # Gemini (Google AI for Developers) – native audio Live API
    GEMINI_API_KEY: Optional[str] = None
    GEMINI_NATIVE_MODEL: str = "gemini-2.5-flash-native-audio-preview-09-2025"
    GEMINI_SAMPLE_RATE: int = 24000
    GEMINI_SYSTEM_INSTRUCTION: Optional[str] = None

    # OpenTelemetry / Grafana
    OTEL_SERVICE_NAME: str = "livekit-service"
    OTEL_SERVICE_NAMESPACE: str = "rox"
    OTEL_RESOURCE_ATTRIBUTES: Optional[str] = None
    OTEL_EXPORTER_OTLP_PROTOCOL: Optional[str] = None
    OTEL_EXPORTER_OTLP_HEADERS: Optional[str] = None
    OTEL_EXPORTER_OTLP_TRACES_HEADERS: Optional[str] = None
    OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: Optional[str] = None
    OTEL_EXPORTER_OTLP_ENDPOINT: Optional[str] = None
    OTEL_EXPORTER_OTLP_INSECURE: bool = False
    OTEL_EXPORTER_OTLP_LOGS_HEADERS: Optional[str] = None
    OTEL_EXPORTER_OTLP_LOGS_ENDPOINT: Optional[str] = None
    GRAFANA_OTLP_HTTP_ENDPOINT: Optional[str] = None
    GRAFANA_OTLP_HEADERS: Optional[str] = None

    # Student metadata override
    STUDENT_TOKEN_METADATA: Optional[str] = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="allow",
    )


_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
