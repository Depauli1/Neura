"""Rich output capture for ``/eval``: DataFrame previews, PNG plots, HTML.

The runtime's display hook and display publisher are swapped for capturing
equivalents while a cell runs, so ``display(...)`` and cell results produce
bounded, structured items in ``execution.outputs`` instead of only text. Plain
``text/plain`` display data keeps its old behavior (it lands on stdout).

The captured HTML is raw by design: the runtime runs with the user's own
permissions, so it is not a security boundary. The VS Code webview sanitizes
HTML against an allowlist before rendering it.
"""

from __future__ import annotations

import base64
import io
import sys
from typing import Any

from IPython.core.displayhook import DisplayHook

from neura_core.execution import (
    FIGURE_DPI,
    MAX_DATAFRAME_PREVIEW_ROWS,
    MAX_FIGURE_BYTES,
    MAX_FIGURES,
    MAX_RICH_HTML_CHARS,
    clip,
)
from neura_core.frames import frame_preview


class QuietResultHook(DisplayHook):
    """Record the last expression without printing an ``Out[n]`` prompt.

    The executor reads the value from ``ExecutionResult.result`` directly and
    appends structured items for DataFrame/plot results, so nothing is printed
    or published here. IPython's underscore/output-history semantics are kept.
    """

    def __call__(self, result=None) -> None:
        self.check_for_underscore()
        if result is not None and not self.quiet():
            self.update_user_ns(result)
            self.fill_exec_result(result)


class DisplayCollector:
    """Stand-in for ``shell.display_pub`` that collects rich display data.

    Implements the small surface IPython/ipykernel touch on a display
    publisher: ``publish``, ``clear_output``, and the ``is_publishing`` flag
    that stream tees consult.
    """

    def __init__(self, items: list[dict], stdout: Any = None) -> None:
        self.items = items
        self.stdout = stdout
        self.is_publishing = False
        self._in_post_execute = False

    def publish(self, data: dict, metadata: dict | None = None, **kwargs: Any) -> None:
        if not isinstance(data, dict):
            return
        html = data.get("text/html")
        png = data.get("image/png")
        if html:
            self.items.append(html_item(html if isinstance(html, str) else str(html)))
            return
        if png:
            item = png_item(png)
            if item is not None:
                self.items.append(item)
            return
        plain = data.get("text/plain")
        if plain and self.stdout is not None:
            try:
                self.stdout.write(plain if isinstance(plain, str) else str(plain))
            except Exception:  # noqa: BLE001 - a closed capture must not fail
                pass

    def clear_output(self, wait: bool = False) -> None:
        """Captured output is not a notebook cell; clearing it is a no-op."""
        return None


def html_item(html: str) -> dict:
    """Bounded HTML item; the webview sanitizes it against an allowlist."""
    truncated = len(html) > MAX_RICH_HTML_CHARS
    return {"kind": "html", "html": clip(html, MAX_RICH_HTML_CHARS), "truncated": truncated}


def png_item(png: Any) -> dict | None:
    """Bounded base64 PNG item, or a note when the image is too large."""
    if isinstance(png, str):
        try:
            raw = base64.b64decode(png, validate=False)
        except Exception:  # noqa: BLE001 - not valid base64
            return None
    else:
        raw = bytes(png)
    if len(raw) > MAX_FIGURE_BYTES:
        return {
            "kind": "text",
            "text": f"[image omitted: {len(raw)} bytes exceeds the "
            f"{MAX_FIGURE_BYTES} byte capture budget]",
            "truncated": True,
        }
    width, height = _png_size(raw)
    return {
        "kind": "image_png",
        "format": "png",
        "data": base64.b64encode(raw).decode("ascii"),
        "bytes": len(raw),
        "width": width,
        "height": height,
        "truncated": False,
    }


def _png_size(raw: bytes) -> tuple[int, int]:
    """Read width/height from the PNG IHDR chunk (0, 0 when unreadable)."""
    if len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n" or raw[12:16] != b"IHDR":
        return 0, 0
    return int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big")


def dataframe_item(obj: Any) -> dict | None:
    """Structured preview item for a pandas DataFrame cell result."""
    if type(obj).__module__ != "pandas" or type(obj).__name__ != "DataFrame":
        return None
    try:
        preview = frame_preview(obj, MAX_DATAFRAME_PREVIEW_ROWS)
    except Exception:  # noqa: BLE001 - exotic frames must not fail the cell
        return None
    return {"kind": "dataframe", **preview}


def figure_item(obj: Any) -> dict | None:
    """PNG item for a matplotlib Figure cell result (mirrors inline backends)."""
    if type(obj).__module__ != "matplotlib.figure" or type(obj).__name__ != "Figure":
        return None
    try:
        import io

        buffer = io.BytesIO()
        obj.savefig(buffer, format="png", dpi=FIGURE_DPI, bbox_inches="tight")
    except Exception:  # noqa: BLE001 - a figure that cannot render is skipped
        return None
    return png_item(buffer.getvalue())


def figure_numbers() -> set[int] | None:
    """Current matplotlib figure numbers, or None when pyplot is not loaded."""
    plt = sys.modules.get("matplotlib.pyplot")
    if plt is None:
        return None
    try:
        return set(plt.get_fignums())
    except Exception:  # noqa: BLE001 - pyplot present but unusable
        return None


def capture_new_figures(before: set[int] | None, items: list[dict]) -> int:
    """Render figures opened during a cell to PNG and close them.

    Closing mirrors notebook inline-backend behavior and prevents the same
    figure from being captured again by the next cell. ``before`` is None when
    pyplot was not imported before the cell, in which case every open figure
    must have been created during it. Any failure is ignored: a broken
    matplotlib install must not fail user code.

    Returns the number of figures dropped because of ``MAX_FIGURES``.
    """
    plt = sys.modules.get("matplotlib.pyplot")
    if plt is None:
        return 0
    try:
        current = set(plt.get_fignums())
    except Exception:  # noqa: BLE001
        return 0
    if before is None:
        before = set()
    fresh = sorted(current - before)
    for number in fresh[:MAX_FIGURES]:
        try:
            figure = plt.figure(number)
            buffer = io.BytesIO()
            figure.savefig(
                buffer,
                format="png",
                dpi=FIGURE_DPI,
                bbox_inches="tight",
            )
            item = png_item(buffer.getvalue())
            if item is not None:
                items.append(item)
        except Exception:  # noqa: BLE001
            continue
    # Close every figure the cell opened, including the dropped ones, so the
    # next cell only captures its own figures.
    for number in fresh:
        try:
            plt.close(number)
        except Exception:  # noqa: BLE001
            continue
    return max(0, len(fresh) - MAX_FIGURES)
