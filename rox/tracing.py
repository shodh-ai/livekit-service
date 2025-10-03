import contextvars
import uuid
from typing import Optional, Dict

_trace_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("trace_id", default=None)

def set_trace_id(trace_id: Optional[str]) -> None:
    _trace_id_var.set(trace_id)

def get_trace_id() -> Optional[str]:
    return _trace_id_var.get()

def ensure_trace_id(existing: Optional[str] = None) -> str:
    tid = existing or get_trace_id() or str(uuid.uuid4())
    _trace_id_var.set(tid)
    return tid

def headers_with_trace(existing: Optional[str] = None) -> Dict[str, str]:
    tid = existing or get_trace_id()
    return {"X-Trace-Id": tid} if tid else {}
