import logging
from opentelemetry import trace as otel_trace
from tracing import get_trace_id


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
        return True


def install_logging_filter() -> None:
    filt = TraceContextFilter()
    root = logging.getLogger()
    for h in root.handlers:
        h.addFilter(filt)
        if h.formatter:
            current = h.formatter._fmt
            if "rox=" not in current and "otel_trace_id" not in current:
                new_fmt = current.rstrip() + " [rox=%(rox_trace_id)s otel=%(otel_trace_id)s span=%(otel_span_id)s]"
                h.setFormatter(logging.Formatter(new_fmt))
