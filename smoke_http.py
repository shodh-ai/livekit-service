# smoke_http.py
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

tp = TracerProvider(resource=Resource.create({"service.name": "local-smoke"}))
tp.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
trace.set_tracer_provider(tp)
tracer = trace.get_tracer("smoke")
with tracer.start_as_current_span("smoke-http-test") as span:
    span.set_attribute("rox.trace_id", "local-http")
print("sent")