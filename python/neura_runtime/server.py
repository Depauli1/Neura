"""The managed runtime's HTTP app, built from the shared Neura surface.

The full implementation lives in :mod:`neura_core.http` so the notebook bridge
(``neura_bridge``) can expose the identical wire contract. This module keeps
the historical import path working.
"""

from __future__ import annotations

from neura_core.execution import (  # noqa: F401 - re-exported for compatibility
    BusyError,
    Executor,
    RuntimeStoppingError,
)
from neura_core.http import (  # noqa: F401 - re-exported for compatibility
    MAX_REQUEST_BYTES,
    EvalRequest,
    InspectRequest,
    ProfileRequest,
    create_app,
)

__all__ = [
    "BusyError",
    "EvalRequest",
    "Executor",
    "InspectRequest",
    "MAX_REQUEST_BYTES",
    "ProfileRequest",
    "RuntimeStoppingError",
    "create_app",
]
