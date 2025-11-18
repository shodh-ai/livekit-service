import logging
import os
from opentelemetry import trace
from opentelemetry.metrics import set_meter_provider
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter as OTLPMetricExporterGRPC
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter as OTLPMetricExporterHTTP
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter as OTLPSpanExporterGRPC
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter as OTLPSpanExporterHTTP
from opentelemetry.instrumentation.aiohttp_client import AioHttpClientInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from config import get_settings

# HTTPX instrumentation is optional; guard import to avoid hard failure if package is absent
try:
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor  # type: ignore
    _HTTPX_INSTR_AVAILABLE = True
except Exception:
    _HTTPX_INSTR_AVAILABLE = False

# Optional: OTel Logs support
try:
    from opentelemetry._logs import set_logger_provider
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.exporter.otlp.proto.grpc._log_exporter import (
        OTLPLogExporter as OTLPLogExporterGRPC,
    )
    from opentelemetry.exporter.otlp.proto.http._log_exporter import (
        OTLPLogExporter as OTLPLogExporterHTTP,
    )
    _OTEL_LOGS_AVAILABLE = True
except Exception:
    _OTEL_LOGS_AVAILABLE = False

logger = logging.getLogger(__name__)


def _parse_headers_env(val: str | None) -> dict:
    if not val:
        return {}
    headers = {}
    for part in val.split(","):
        if not part:
            continue
        if "=" in part:
            k, v = part.split("=", 1)
            headers[k.strip()] = v.strip()
    return headers

def _parse_attributes_env(val: str | None) -> dict:
    if not val:
        return {}
    attrs = {}
    for part in val.split(","):
        if not part:
            continue
        if "=" in part:
            k, v = part.split("=", 1)
            attrs[k.strip()] = v.strip()
    return attrs


def init_tracing(service_name: str | None = None, enable_logs: bool | None = None) -> None:
    """Initialize OpenTelemetry tracing and optionally logs; supports Grafana Cloud OTLP HTTP mapping.

    Args:
        service_name: override OTEL service name.
        enable_logs: if True, export std logging to OTEL; if False, skip OTel logs; if None, use env OTEL_ENABLE_LOGS (default True for workers).
    """
    settings = get_settings()
    svc = service_name or settings.OTEL_SERVICE_NAME
    if enable_logs is None:
        try:
            # Default True unless explicitly set to 'false'
            env_val = os.environ.get("OTEL_ENABLE_LOGS", "true").strip().lower()
            enable_logs = env_val not in ("0", "false", "no")
        except Exception:
            enable_logs = True

    # Resource with service name + merged OTEL_RESOURCE_ATTRIBUTES
    base_attrs = {
        "service.name": svc,
        "service.namespace": settings.OTEL_SERVICE_NAMESPACE,
    }
    base_attrs.update(_parse_attributes_env(settings.OTEL_RESOURCE_ATTRIBUTES))
    resource = Resource.create(base_attrs)

    # Prefer Grafana mapping if provided
    grafana_http_traces = settings.GRAFANA_OTLP_HTTP_ENDPOINT
    grafana_headers = _parse_headers_env(settings.GRAFANA_OTLP_HEADERS)

    protocol_env = settings.OTEL_EXPORTER_OTLP_PROTOCOL
    protocol = (protocol_env or ("http/protobuf" if grafana_http_traces else "grpc")).lower()

    generic_headers = _parse_headers_env(settings.OTEL_EXPORTER_OTLP_HEADERS)
    traces_headers = (
        _parse_headers_env(settings.OTEL_EXPORTER_OTLP_TRACES_HEADERS)
        or generic_headers
        or grafana_headers
    )

    # Tracer provider
    provider = TracerProvider(resource=resource)

    if protocol in ("http", "http/protobuf"):
        endpoint = (
            settings.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT
            or settings.OTEL_EXPORTER_OTLP_ENDPOINT
            or grafana_http_traces
            or "http://localhost:4318/v1/traces"
        )
        span_exporter = OTLPSpanExporterHTTP(endpoint=endpoint, headers=traces_headers or None)
        try:
            logger.info(f"OTel traces: HTTP exporter configured -> {endpoint}")
        except Exception:
            pass
    else:
        endpoint = (
            settings.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT
            or settings.OTEL_EXPORTER_OTLP_ENDPOINT
            or "http://localhost:4317"
        )
        insecure = bool(settings.OTEL_EXPORTER_OTLP_INSECURE)
        span_exporter = OTLPSpanExporterGRPC(endpoint=endpoint, insecure=insecure, headers=traces_headers or None)
        try:
            logger.info(f"OTel traces: gRPC exporter configured -> {endpoint} (insecure={insecure})")
        except Exception:
            pass

    provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(provider)

    try:
        if protocol in ("http", "http/protobuf"):
            metrics_endpoint = (
                settings.OTEL_EXPORTER_OTLP_METRICS_ENDPOINT
                if hasattr(settings, "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT") else None
            ) or settings.OTEL_EXPORTER_OTLP_ENDPOINT or (
                grafana_http_traces.replace("/v1/traces", "/v1/metrics") if grafana_http_traces and "/v1/traces" in grafana_http_traces else None
            ) or "http://localhost:4318/v1/metrics"
            metrics_exporter = OTLPMetricExporterHTTP(endpoint=metrics_endpoint, headers=(
                _parse_headers_env(getattr(settings, "OTEL_EXPORTER_OTLP_METRICS_HEADERS", None))
            ) or traces_headers or None)
        else:
            metrics_endpoint = (
                settings.OTEL_EXPORTER_OTLP_METRICS_ENDPOINT
                if hasattr(settings, "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT") else None
            ) or settings.OTEL_EXPORTER_OTLP_ENDPOINT or "http://localhost:4317"
            metrics_exporter = OTLPMetricExporterGRPC(endpoint=metrics_endpoint, insecure=bool(settings.OTEL_EXPORTER_OTLP_INSECURE), headers=traces_headers or None)
        reader = PeriodicExportingMetricReader(metrics_exporter)
        mp = MeterProvider(metric_readers=[reader], resource=resource)
        set_meter_provider(mp)
    except Exception:
        pass

    # --- OTel Logs export (optional) ---
    try:
        if _OTEL_LOGS_AVAILABLE and enable_logs:
            logs_headers = (
                _parse_headers_env(settings.OTEL_EXPORTER_OTLP_LOGS_HEADERS)
                or generic_headers
                or grafana_headers
            )
            if protocol in ("http", "http/protobuf"):
                logs_endpoint = (
                    settings.OTEL_EXPORTER_OTLP_LOGS_ENDPOINT
                    or settings.OTEL_EXPORTER_OTLP_ENDPOINT
                    or (
                        grafana_http_traces.replace("/v1/traces", "/v1/logs")
                        if grafana_http_traces and "/v1/traces" in grafana_http_traces
                        else None
                    )
                    or "http://localhost:4318/v1/logs"
                )
                log_exporter = OTLPLogExporterHTTP(endpoint=logs_endpoint, headers=logs_headers or None)
                try:
                    logger.info(f"OTel logs: HTTP exporter configured -> {logs_endpoint}")
                except Exception:
                    pass
            else:
                logs_endpoint = (
                    settings.OTEL_EXPORTER_OTLP_LOGS_ENDPOINT
                    or settings.OTEL_EXPORTER_OTLP_ENDPOINT
                    or "http://localhost:4317"
                )
                insecure_logs = bool(settings.OTEL_EXPORTER_OTLP_INSECURE)
                log_exporter = OTLPLogExporterGRPC(endpoint=logs_endpoint, insecure=insecure_logs, headers=logs_headers or None)
                try:
                    logger.info(f"OTel logs: gRPC exporter configured -> {logs_endpoint} (insecure={insecure_logs})")
                except Exception:
                    pass

            log_provider = LoggerProvider(resource=resource)
            set_logger_provider(log_provider)
            # Increase buffers to reduce drops under bursty logging
            log_provider.add_log_record_processor(
                BatchLogRecordProcessor(
                    log_exporter,
                    max_queue_size=4096,
                    schedule_delay_millis=3000,
                    export_timeout_millis=10000,
                    max_export_batch_size=512,
                )
            )

            # Attach handler to root so std logging flows to OTel logs
            root_logger = logging.getLogger()
            if not any(isinstance(h, LoggingHandler) for h in root_logger.handlers):
                # Export logs at INFO+ to avoid DEBUG spam in Grafana
                root_logger.addHandler(LoggingHandler(level=logging.INFO))
            try:
                # Ensure INFO logs are not filtered out by a WARNING default
                root_logger.setLevel(logging.INFO)
            except Exception:
                pass

            # Also attach handler directly to 'rox' logger as a safety net in case
            # third-party code replaces root handlers after init
            rox_logger = logging.getLogger("rox")
            if not any(isinstance(h, LoggingHandler) for h in rox_logger.handlers):
                rox_logger.addHandler(LoggingHandler(level=logging.INFO))
            try:
                rox_logger.setLevel(logging.INFO)
                rox_logger.propagate = True
            except Exception:
                pass

            # Attach to common rox.* modules explicitly
            for name in (
                "rox.agent",
                "rox.entrypoint",
                "rox.frontend_client",
                "rox.langgraph_client",
                "rox.server",
            ):
                try:
                    lg = logging.getLogger(name)
                    if not any(isinstance(h, LoggingHandler) for h in lg.handlers):
                        lg.addHandler(LoggingHandler(level=logging.INFO))
                    lg.setLevel(logging.INFO)
                    lg.propagate = True
                except Exception:
                    pass

            # Attach to 'livekit' logger as well to capture its INFO logs in Grafana
            lk_root = logging.getLogger("livekit")
            if not any(isinstance(h, LoggingHandler) for h in lk_root.handlers):
                lk_root.addHandler(LoggingHandler(level=logging.INFO))
            try:
                lk_root.setLevel(logging.INFO)
                lk_root.propagate = True
            except Exception:
                pass
    except Exception:
        pass

    # Instrument clients and logging
    try:
        AioHttpClientInstrumentor().instrument()
    except Exception:
        pass
    try:
        if _HTTPX_INSTR_AVAILABLE:
            HTTPXClientInstrumentor().instrument()
    except Exception:
        pass
    try:
        # Do not override console formatting; we already have basicConfig.
        LoggingInstrumentor().instrument(set_logging_format=False)
    except Exception:
        pass

    # Tame noisy libraries to reduce console/Grafana noise
    try:
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)
    except Exception:
        pass
    try:
        logging.getLogger("opentelemetry").setLevel(logging.WARNING)
        logging.getLogger("opentelemetry.exporter").setLevel(logging.ERROR)
        logging.getLogger("opentelemetry.instrumentation").setLevel(logging.ERROR)
    except Exception:
        pass
    try:
        logging.getLogger("asyncio").setLevel(logging.INFO)
    except Exception:
        pass
    try:
        logging.getLogger("livekit").setLevel(logging.INFO)
        # Make sure propagation is on for both 'livekit' and 'livekit.agents'
        try:
            logging.getLogger("livekit").propagate = True
            logging.getLogger("livekit.agents").propagate = True
        except Exception:
            pass
    except Exception:
        pass

    # Normalize livekit.agents logger (dev mode sometimes adds its own handler)
    try:
        lk_logger = logging.getLogger("livekit.agents")
        # Remove custom handlers (keep export via root)
        lk_logger.handlers = []
        lk_logger.propagate = True
        lk_logger.setLevel(logging.INFO)
    except Exception:
        pass

    # Deduplicate root StreamHandlers (some libs add multiple handlers to stdout/stderr)
    try:
        root = logging.getLogger()
        seen = set()
        new_handlers = []
        for h in root.handlers:
            key = (type(h), getattr(h, "stream", None))
            if key in seen and hasattr(h, "stream"):
                continue
            seen.add(key)
            new_handlers.append(h)
        root.handlers = new_handlers
    except Exception:
        pass
