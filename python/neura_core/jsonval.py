"""JSON-safe conversion for values that come out of user namespaces.

pandas/numpy scalars, timestamps, sets, bytes and NaN/Infinity are not JSON
serializable (and ``allow_nan=False`` rejects NaN/Infinity outright), so every
value crossing the runtime API is normalized here. Objects that are none of the
above fall back to a bounded ``repr``.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

MAX_VALUE_REPR_CHARS = 1000
MAX_SEQUENCE_ITEMS = 1000


def json_safe(value: Any, limit: int = MAX_VALUE_REPR_CHARS) -> Any:
    """Convert a runtime value into something ``json.dumps`` accepts."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _bounded(value, limit)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    # numpy scalars (np.int64, np.float64, np.bool_) and 0-d arrays.
    item = getattr(value, "item", None)
    if callable(item) and getattr(value, "shape", None) == ():
        try:
            return json_safe(item(), limit)
        except Exception:  # noqa: BLE001 - fall through to repr
            pass
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, Decimal):
        return float(value) if value.is_finite() else None
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{type(value).__name__} {len(bytes(value))} bytes>"
    if isinstance(value, (set, frozenset, tuple)):
        try:
            return [json_safe(item, limit) for item in value]
        except Exception:  # noqa: BLE001
            return _bounded_repr(value, limit)
    if isinstance(value, list):
        return [json_safe(item, limit) for item in value]
    if isinstance(value, dict):
        return {str(key): json_safe(item, limit) for key, item in value.items()}
    if isinstance(value, complex):
        return {"real": value.real, "imag": value.imag}
    sequence = _small_sequence(value)
    if sequence is not None:
        return sequence
    return _bounded_repr(value, limit)


def _small_sequence(value: Any) -> list | None:
    """Convert a small numpy array/Series/pandas Index to a JSON list."""
    if type(value).__module__ not in ("numpy", "pandas", "pandas.core.arrays"):
        return None
    tolist = getattr(value, "tolist", None)
    if not callable(tolist):
        return None
    try:
        items = tolist()
    except Exception:  # noqa: BLE001 - fall back to a bounded repr
        return None
    if not isinstance(items, list) or len(items) > MAX_SEQUENCE_ITEMS:
        return None
    return [json_safe(item) for item in items]


def _bounded_repr(value: Any, limit: int) -> str:
    try:
        text = repr(value)
    except Exception as exc:  # noqa: BLE001 - a broken repr must not fail the request
        return f"<repr failed: {type(exc).__name__}>"
    return _bounded(text, limit)


def _bounded(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - len("... [truncated]")] + "... [truncated]"
