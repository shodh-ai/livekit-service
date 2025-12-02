import contextvars
from typing import Optional, Dict

_session_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("session_id", default=None)
_user_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("user_id", default=None)

def set_request_context(session_id: Optional[str], user_id: Optional[str]) -> None:
    _session_id_var.set(session_id)
    _user_id_var.set(user_id)

def get_request_context() -> Dict[str, Optional[str]]:
    return {"session_id": _session_id_var.get(), "user_id": _user_id_var.get()}
