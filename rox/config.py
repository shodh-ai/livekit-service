from __future__ import annotations
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # =================================================================
    # 1. CORE LIVEKIT CONFIGURATION
    # Required for the worker to connect to the LiveKit Cloud/Server
    # =================================================================
    LIVEKIT_URL: Optional[str] = None
    LIVEKIT_API_KEY: Optional[str] = None
    LIVEKIT_API_SECRET: Optional[str] = None
    
    # Used if running a health check server or basic HTTP endpoints
    PORT: int = 8080

    # =================================================================
    # 2. SESSION LIFECYCLE & TIMEOUTS (entrypoint.py)
    # =================================================================
    # How long to wait for a student to reconnect before killing the pod
    STUDENT_DISCONNECT_GRACE_SEC: float = 12.0
    
    # How long to wait for a student to join after room creation
    ROX_NO_SHOW_TIMEOUT_SECONDS: int = 60
    
    # Used to override metadata for testing (entrypoint.py)
    STUDENT_TOKEN_METADATA: Optional[str] = None

    # Browser Pod coordination
    BROWSER_JOIN_WAIT_SEC: float = 6.0
    BROWSER_JOIN_WATCH_SEC: float = 60.0

    # =================================================================
    # 3. EXTERNAL SERVICES (Deepgram)
    # =================================================================
    DEEPGRAM_API_KEY: Optional[str] = None
    
    # Mic debounce logic (agent.py)
    MIC_ENABLE_DEBOUNCE_SEC: float = 0.5

    # =================================================================
    # 4. FRONTEND COMMUNICATION (frontend_client.py)
    # =================================================================
    # Timeout for RPC calls to the UI (e.g., "Get Code Block")
    FRONTEND_RPC_TIMEOUT_SEC: float = 15.0

    # =================================================================
    # 5. BRAIN / LANGGRAPH SERVICE (langgraph_client.py)
    # =================================================================
    # The agent looks for these in order to find the Brain service
    LANGGRAPH_TUTOR_URL: Optional[str] = None
    LANGGRAPH_API_URL: Optional[str] = None
    MY_CUSTOM_AGENT_URL: Optional[str] = None
    
    # Browser Pod / VNC URLs for taking screenshots
    VNC_LISTENER_HTTP_URL: Optional[str] = None
    BROWSER_POD_HTTP_URL: Optional[str] = None

    # Fine-tuning HTTP connection pooling and timeouts
    LANGGRAPH_TOTAL_TIMEOUT: float = 120.0
    LANGGRAPH_CONNECT_TIMEOUT: float = 10.0
    LANGGRAPH_SOCK_CONNECT_TIMEOUT: Optional[float] = None
    LANGGRAPH_READ_TIMEOUT: float = 110.0
    
    # Resilience / Retries
    LANGGRAPH_MAX_RETRIES: int = 3
    LANGGRAPH_BACKOFF_BASE: float = 1.5
    
    # Feature Flags
    LANGGRAPH_INCLUDE_VISUAL_CONTEXT: bool = True
    LANGGRAPH_ATTACH_BUFFERS: bool = True

    # =================================================================
    # 6. OBSERVABILITY (main.py / OpenTelemetry)
    # =================================================================
    OTEL_SERVICE_NAME: str = "livekit-service"
    OTEL_SERVICE_NAMESPACE: str = "rox"
    OTEL_RESOURCE_ATTRIBUTES: Optional[str] = None
    
    # Protocol: "grpc" or "http/protobuf"
    OTEL_EXPORTER_OTLP_PROTOCOL: Optional[str] = None
    OTEL_EXPORTER_OTLP_INSECURE: bool = False
    
    # General Endpoint (used if specific ones aren't set)
    OTEL_EXPORTER_OTLP_ENDPOINT: Optional[str] = None
    OTEL_EXPORTER_OTLP_HEADERS: Optional[str] = None

    # Traces
    OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: Optional[str] = None
    OTEL_EXPORTER_OTLP_TRACES_HEADERS: Optional[str] = None
    
    # Metrics
    OTEL_EXPORTER_OTLP_METRICS_ENDPOINT: Optional[str] = None
    OTEL_EXPORTER_OTLP_METRICS_HEADERS: Optional[str] = None
    
    # Logs
    OTEL_ENABLE_LOGS: bool = True
    OTEL_EXPORTER_OTLP_LOGS_ENDPOINT: Optional[str] = None
    OTEL_EXPORTER_OTLP_LOGS_HEADERS: Optional[str] = None

    # Grafana Cloud specific shortcuts
    GRAFANA_OTLP_HTTP_ENDPOINT: Optional[str] = None
    GRAFANA_OTLP_HEADERS: Optional[str] = None

    # =================================================================
    # 7. REDIS (Optional / Infrastructure)
    # Useful for distributed locks if you scale horizontally
    # =================================================================
    REDIS_URL: Optional[str] = None
    REDIS_HOST: Optional[str] = None
    REDIS_PORT: int = 6379
    
    # Sticky worker logic configuration
    AGENT_LOCK_TTL_SEC: int = 300
    AGENT_LOCK_HEARTBEAT_SEC: int = 60
    ENABLE_DEBUG_LOCKS_ENDPOINT: bool = False
    ROOM_OWNER_TTL_SEC: int = 3600
    JOB_QUEUE_GENERAL: str = "job-queue:general"
    JOB_QUEUE_PREFIX: str = "job-queue:"

    # Standard Pydantic configuration to read from .env
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="allow",  # Allows extra fields in .env without crashing
    )

# Singleton pattern to prevent re-parsing env vars on every call
_settings: Optional[Settings] = None

def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
