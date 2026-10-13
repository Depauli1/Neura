"""Shared execution contract for both Neura backends.

``neura_runtime`` (managed ``.py`` mode) and ``neura_bridge`` (``.ipynb`` attach
mode) implement the same interface so the HTTP surface in
:mod:`neura_core.http` and the extension can treat them interchangeably.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

DEFAULT_TIMEOUT_S = 30.0
MAX_TIMEOUT_S = 3600.0
MAX_OUTPUT_CHARS = 64 * 1024  # per stream
MAX_RESULT_REPR_CHARS = 2000
MAX_TRACEBACK_CHARS = 16 * 1024
MAX_MESSAGE_CHARS = 4000
INTERRUPT_GRACE_S = 2.0

# Budgets for the inspection surface (list_vars/inspect_df/profile_df).
DEFAULT_INSPECT_TIMEOUT_S = 10.0
MAX_INSPECT_TIMEOUT_S = 60.0
MAX_VARS = 200
MAX_VAR_PREVIEW_CHARS = 200
MAX_INSPECT_ROWS = 100
MAX_INSPECT_VALUE_CHARS = 1000
MAX_PROFILE_ROWS = 10_000_000
MAX_PROFILE_SAMPLE_ROWS = 50
MAX_PROFILE_TOP_K = 50
MAX_PROFILE_PAYLOAD_BYTES = 512 * 1024
PROFILE_TIME_BUDGET_S = 5.0

# Budgets for rich output capture attached to /eval.
MAX_RICH_HTML_CHARS = 64 * 1024
MAX_DATAFRAME_PREVIEW_ROWS = 50
MAX_FIGURES = 3
MAX_FIGURE_BYTES = 200 * 1024
FIGURE_DPI = 100


class BusyError(RuntimeError):
    """Another execution is already active (HTTP 409)."""


class RuntimeStoppingError(RuntimeError):
    """The backend is shutting down (HTTP 503)."""


class Executor(ABC):
    """One persistent Python namespace with a single active execution.

    Implementations serialize every method through the same execution slot, so
    the namespace is never read while user code mutates it. ``eval`` runs user
    code; the remaining methods are bounded, read-only inspections.
    """

    @property
    @abstractmethod
    def busy(self) -> bool: ...

    @abstractmethod
    def eval(self, code: str, timeout_s: float = DEFAULT_TIMEOUT_S, cwd: str | None = None) -> dict:
        """Run code in the persistent namespace; return a response envelope."""

    @abstractmethod
    def interrupt(self) -> bool:
        """Request an interrupt; return False when nothing is active."""

    @abstractmethod
    def list_vars(self, timeout_s: float = DEFAULT_INSPECT_TIMEOUT_S) -> dict:
        """Summarize user variables in the namespace."""

    @abstractmethod
    def inspect_df(
        self, name: str, n: int = 5, timeout_s: float = DEFAULT_INSPECT_TIMEOUT_S
    ) -> dict:
        """Return a small, JSON-safe preview of a pandas DataFrame variable."""

    @abstractmethod
    def profile_df(
        self,
        name: str,
        config: dict | None = None,
        timeout_s: float = DEFAULT_INSPECT_TIMEOUT_S,
    ) -> dict:
        """Return a budgeted profile of a pandas DataFrame variable."""

    @abstractmethod
    def last_error(self) -> dict:
        """Return the most recent structured execution error, if any."""

    @abstractmethod
    def stop(self) -> None:
        """Gracefully stop the backend's own serving loop."""


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = "... [truncated]"
    return text[: limit - len(marker)] + marker


def any_dict(value: Any) -> bool:  # pragma: no cover - tiny typing helper
    return isinstance(value, dict)


def execution_payload(
    *,
    stdout: str,
    stderr: str = "",
    stdout_truncated: bool = False,
    stderr_truncated: bool = False,
    result_repr: str | None = None,
    result_repr_truncated: bool = False,
    outputs: list[dict] | None = None,
    duration_ms: int = 0,
    state_changed: bool = False,
) -> dict:
    """Build the ``execution`` object shared by every backend.

    Both ``neura_runtime`` and ``neura_bridge`` emit exactly this shape so the
    extension and the agent layer do not need to know which backend answered.
    """
    items = list(outputs or [])
    return {
        "stdout": stdout,
        "stderr": stderr,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "result_repr": result_repr,
        "result_kind": "none" if result_repr is None else "value",
        "result_repr_truncated": result_repr_truncated,
        "state_changed": state_changed,
        "rich_truncated": any(item.get("truncated") for item in items),
        "duration_ms": duration_ms,
        "outputs": items,
    }
