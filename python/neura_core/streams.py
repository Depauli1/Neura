"""Bounded stdout/stderr capture shared by both backends."""

from __future__ import annotations

import io

from neura_core.execution import MAX_OUTPUT_CHARS

_MARKER = "... [truncated]"


class BoundedTextIO(io.TextIOBase):
    """Keep only the first N characters, but report every write as consumed.

    Writers see the full length they wrote (so ``print`` and C-level writers
    never see a short write), while memory stays bounded. When the limit is
    exceeded the captured text is clipped once, with a visible marker.
    """

    def __init__(self, limit: int = MAX_OUTPUT_CHARS) -> None:
        self._buffer = io.StringIO()
        self._limit = max(0, limit)
        self._size = 0
        self.truncated = False

    @property
    def encoding(self) -> str:
        return "utf-8"

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        remaining = self._limit - self._size
        kept = text[:remaining] if remaining > 0 else ""
        self._buffer.write(kept)
        self._size += len(kept)
        self.truncated |= len(text) > remaining
        return len(text)

    def getvalue(self) -> str:
        text = self._buffer.getvalue()
        if not self.truncated:
            return text
        # Something was dropped, so the buffer is exactly at the limit: swap
        # the tail for a visible marker rather than growing past the budget.
        return text[: self._limit - len(_MARKER)] + _MARKER
