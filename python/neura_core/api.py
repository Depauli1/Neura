"""Shared HTTP API constants and response-envelope helpers.

Both backends (``neura_runtime`` and ``neura_bridge``) MUST produce identical
response envelopes so the VS Code extension and the agent layer can treat them
interchangeably (see ``docs/api/runtime-api.md``).
"""

API_VERSION = "0.1.0"


def ok(**fields) -> dict:
    """Success envelope: ``{"ok": true, "version": ..., ...fields}``."""
    return {"ok": True, "version": API_VERSION, **fields}


def error(error_type: str, message: str, traceback: str | None = None, **fields) -> dict:
    """Error envelope: ``{"ok": false, "version": ..., "error": {...}, ...fields}``."""
    err = {"type": error_type, "message": message}
    if traceback is not None:
        err["traceback"] = traceback
    return {"ok": False, "version": API_VERSION, "error": err, **fields}
