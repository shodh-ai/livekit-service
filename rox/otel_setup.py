import os
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter as OTLPSpanExporterGRPC
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter as OTLPSpanExporterHTTP
from opentelemetry.instrumentation.aiohttp_client import AioHttpClientInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor


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


def init_tracing(service_name: str | None = None) -> None:
    """Initialize OpenTelemetry tracing and common instrumentations.

    Respects standard OTEL_* environment variables; defaults to OTLP gRPC at http://localhost:4317.
    """
    svc = service_name or os.getenv("OTEL_SERVICE_NAME", "livekit-service")

    # Resource with service name
    resource = Resource.create({
        "service.name": svc,
        "service.namespace": os.getenv("OTEL_SERVICE_NAMESPACE", "rox"),
    })

    # Tracer provider
    provider = TracerProvider(resource=resource)

    # OTLP exporter (auto: gRPC default, HTTP if protocol=http/protobuf)
    protocol = os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc").lower()
    headers = _parse_headers_env(os.getenv("OTEL_EXPORTER_OTLP_HEADERS"))
    if protocol in ("http", "http/protobuf"):
        # Grafana Cloud Quickstart commonly provides endpoint like https://.../otlp
        endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318/v1/traces")
        exporter = OTLPSpanExporterHTTP(endpoint=endpoint, headers=headers or None)
    else:
        endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
        insecure = os.getenv("OTEL_EXPORTER_OTLP_INSECURE", "false").lower() == "true"
        exporter = OTLPSpanExporterGRPC(endpoint=endpoint, insecure=insecure, headers=headers or None)
    processor = BatchSpanProcessor(exporter)
    provider.add_span_processor(processor)

    trace.set_tracer_provider(provider)

    # Instrument clients and logging
    try:
        AioHttpClientInstrumentor().instrument()
    except Exception:
        pass
    try:
        LoggingInstrumentor().instrument(set_logging_format=True)
    except Exception:
        pass
