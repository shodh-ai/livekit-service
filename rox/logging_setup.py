import logging
from opentelemetry import trace as otel_trace
from tracing import get_trace_id
try:
    from .request_context import get_request_context
except Exception:
    from request_context import get_request_context


class TraceContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Custom app trace id propagated over headers
        try:
            record.rox_trace_id = get_trace_id() or "-"
        except Exception:
            record.rox_trace_id = "-"
        # Active OpenTelemetry trace id in hex
        try:
            span = otel_trace.get_current_span()
            ctx = span.get_span_context() if span else None
            if ctx and ctx.is_valid:
                record.otel_trace_id = format(ctx.trace_id, "032x")
                record.otel_span_id = format(ctx.span_id, "016x")
            else:
                record.otel_trace_id = "-"
                record.otel_span_id = "-"
        except Exception:
            record.otel_trace_id = "-"
            record.otel_span_id = "-"
        # Add session and student context
        try:
            context = get_request_context()
            record.session_id = context.get("session_id") or "-"
            record.student_id = context.get("student_id") or "-"
        except Exception:
            record.session_id = "-"
            record.student_id = "-"
        return True


def install_logging_filter() -> None:
    filt = TraceContextFilter()
    root = logging.getLogger()
    for h in root.handlers:
        h.addFilter(filt)
        if h.formatter:
            current = h.formatter._fmt

            needs_session = ("session_id" not in current) and ("session=" not in current)
            needs_student = ("student_id" not in current) and ("student=" not in current)
            needs_trace = ("otel_trace_id" not in current) and ("otel=" not in current)
            needs_rox = ("rox_trace_id" not in current) and ("rox=" not in current)
            needs_span = ("otel_span_id" not in current) and ("span=" not in current)

            suffix_parts = []
            if needs_session or needs_student:
                suffix_parts.append("session=%(session_id)s student=%(student_id)s")
            if needs_rox:
                suffix_parts.append("rox=%(rox_trace_id)s")
            if needs_trace:
                suffix_parts.append("otel=%(otel_trace_id)s")
            if needs_span:
                suffix_parts.append("span=%(otel_span_id)s")

            needs_json_payload = "json_payload" not in current
            new_fmt = current.rstrip()
            if suffix_parts:
                new_fmt = new_fmt + " [" + " ".join(suffix_parts) + "]"
            if needs_json_payload:
                new_fmt = new_fmt + " %(json_payload)s"

            class JsonFormatter(logging.Formatter):
                def format(self, record: logging.LogRecord) -> str:
                    if not hasattr(record, "json_payload"):
                        record.json_payload = ""
                    return super().format(record)

            if new_fmt != current:
                h.setFormatter(JsonFormatter(new_fmt))
