import contextvars
from typing import Optional, Dict, Any

_session_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("session_id", default=None)
_student_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("student_id", default=None)

def set_request_context(session_id: Optional[str], student_id: Optional[str]) -> None:
    if session_id:
        _session_id_var.set(session_id)
    if student_id:
        _student_id_var.set(student_id)

def get_request_context() -> Dict[str, Any]:
    return {
        "session_id": _session_id_var.get(),
        "student_id": _student_id_var.get(),
    }
