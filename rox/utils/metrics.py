import time
from typing import Dict, Any

_redis_counters = {
    "write_success": 0,
    "write_error": 0,
}

_last_latencies_ms: Dict[str, float] = {
    "write": 0.0,
}

def inc(key: str, amount: int = 1) -> None:
    if key in _redis_counters:
        _redis_counters[key] += amount


def set_latency(op: str, ms: float) -> None:
    if op in _last_latencies_ms:
        _last_latencies_ms[op] = ms


def snapshot() -> Dict[str, Any]:
    return {
        **_redis_counters,
        "latency_ms": dict(_last_latencies_ms),
        "timestamp": time.time(),
    }
