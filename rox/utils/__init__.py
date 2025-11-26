# livekit-service/rox/utils/__init__.py

import hashlib
import re


def dns_safe_session_id(raw: str) -> str:
    """Convert LiveKit room name into the K8s Service DNS name.

    MUST match the logic in session-manager/main.py.
    """
    if not raw:
        return "sess"

    s = raw.lower()
    s = re.sub(r"[^a-z0-9-]", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")

    if not s:
        s = "sess"

    max_label_len = 63
    if len(s) > max_label_len:
        h = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
        base_len = max_label_len - 9
        s = f"{s[:base_len]}-{h}"

    return s
