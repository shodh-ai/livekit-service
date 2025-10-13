import os
import logging
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter as OTLPSpanExporterGRPC
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter as OTLPSpanExporterHTTP
from opentelemetry.instrumentation.aiohttp_client import AioHttpClientInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor

# Metrics support
try:
    from opentelemetry import metrics
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter as OTLPMetricExporterGRPC
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter as OTLPMetricExporterHTTP
    _OTEL_METRICS_AVAILABLE = True
except Exception:
    _OTEL_METRICS_AVAILABLE = False

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


def init_tracing(service_name: str | None = None) -> None:
    """Initialize OpenTelemetry tracing and logs; support Grafana Cloud OTLP HTTP mapping."""
    svc = service_name or os.getenv("OTEL_SERVICE_NAME", "livekit-service")

    # Resource with service name + merged OTEL_RESOURCE_ATTRIBUTES
    base_attrs = {
        "service.name": svc,
        "service.namespace": os.getenv("OTEL_SERVICE_NAMESPACE", "rox"),
    }
    base_attrs.update(_parse_attributes_env(os.getenv("OTEL_RESOURCE_ATTRIBUTES")))
    resource = Resource.create(base_attrs)

    # Prefer Grafana mapping if provided
    grafana_http_traces = os.getenv("GRAFANA_OTLP_HTTP_ENDPOINT")
    grafana_headers = _parse_headers_env(os.getenv("GRAFANA_OTLP_HEADERS"))

    protocol_env = os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL")
    protocol = (protocol_env or ("http/protobuf" if grafana_http_traces else "grpc")).lower()

    generic_headers = _parse_headers_env(os.getenv("OTEL_EXPORTER_OTLP_HEADERS"))
    traces_headers = (
        _parse_headers_env(os.getenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS"))
        or generic_headers
        or grafana_headers
    )

    # Tracer provider
    provider = TracerProvider(resource=resource)

    if protocol in ("http", "http/protobuf"):
        endpoint = (
            os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
            or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
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
            os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
            or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
            or "http://localhost:4317"
        )
        insecure = os.getenv("OTEL_EXPORTER_OTLP_INSECURE", "false").lower() == "true"
        span_exporter = OTLPSpanExporterGRPC(endpoint=endpoint, insecure=insecure, headers=traces_headers or None)
        try:
            logger.info(f"OTel traces: gRPC exporter configured -> {endpoint} (insecure={insecure})")
        except Exception:
            pass

    provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(provider)

    # --- OTel Metrics export (optional) ---
    try:
        if _OTEL_METRICS_AVAILABLE:
            metrics_headers = (
                _parse_headers_env(os.getenv("OTEL_EXPORTER_OTLP_METRICS_HEADERS"))
                or generic_headers
                or grafana_headers
            )
            if protocol in ("http", "http/protobuf"):
                metrics_endpoint = (
                    os.getenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT")
                    or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
                    or (
                        grafana_http_traces.replace("/v1/traces", "/v1/metrics")
                        if grafana_http_traces and "/v1/traces" in grafana_http_traces
                        else None
                    )
                    or "http://localhost:4318/v1/metrics"
                )
                metric_exporter = OTLPMetricExporterHTTP(endpoint=metrics_endpoint, headers=metrics_headers or None)
                try:
                    logger.info(f"OTel metrics: HTTP exporter configured -> {metrics_endpoint}")
                except Exception:
                    pass
            else:
                metrics_endpoint = (
                    os.getenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT")
                    or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
                    or "http://localhost:4317"
                )
                insecure_metrics = os.getenv("OTEL_EXPORTER_OTLP_INSECURE", "false").lower() == "true"
                metric_exporter = OTLPMetricExporterGRPC(endpoint=metrics_endpoint, insecure=insecure_metrics, headers=metrics_headers or None)
                try:
                    logger.info(f"OTel metrics: gRPC exporter configured -> {metrics_endpoint} (insecure={insecure_metrics})")
                except Exception:
                    pass

            metric_reader = PeriodicExportingMetricReader(metric_exporter, export_interval_millis=60000)
            meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
            metrics.set_meter_provider(meter_provider)
    except Exception:
        pass

    # --- OTel Logs export (optional) ---
    try:
        if _OTEL_LOGS_AVAILABLE:
            logs_headers = (
                _parse_headers_env(os.getenv("OTEL_EXPORTER_OTLP_LOGS_HEADERS"))
                or generic_headers
                or grafana_headers
            )
            if protocol in ("http", "http/protobuf"):
                logs_endpoint = (
                    os.getenv("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT")
                    or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
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
                    os.getenv("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT")
                    or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
                    or "http://localhost:4317"
                )
                insecure_logs = os.getenv("OTEL_EXPORTER_OTLP_INSECURE", "false").lower() == "true"
                log_exporter = OTLPLogExporterGRPC(endpoint=logs_endpoint, insecure=insecure_logs, headers=logs_headers or None)
                try:
                    logger.info(f"OTel logs: gRPC exporter configured -> {logs_endpoint} (insecure={insecure_logs})")
                except Exception:
                    pass

            log_provider = LoggerProvider(resource=resource)
            set_logger_provider(log_provider)
            log_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))

            # Attach handler to root so std logging flows to OTel logs
            root_logger = logging.getLogger()
            if not any(isinstance(h, LoggingHandler) for h in root_logger.handlers):
                root_logger.addHandler(LoggingHandler(level=logging.NOTSET))
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
        LoggingInstrumentor().instrument(set_logging_format=True)
    except Exception:
        pass
