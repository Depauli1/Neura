"""Variable listing for the runtime/bridge inspection surface."""

from __future__ import annotations

import types

from neura_core.execution import MAX_VAR_PREVIEW_CHARS, MAX_VARS, clip
from neura_core.jsonval import json_safe

_PRIVATE_PREFIX = "_"
# IPython injects these into user_ns; they are runtime plumbing, not user data.
_IPYTHON_BUILTINS = frozenset({"In", "Out", "get_ipython", "exit", "quit"})


def _kind(value: object) -> str:
    module = type(value).__module__ or ""
    if module.startswith("pandas"):
        return "dataframe" if type(value).__name__ == "DataFrame" else "series"
    if module.startswith("numpy"):
        return "array"
    if module.startswith("matplotlib.figure"):
        return "figure"
    return "value"


def _preview(value: object) -> str:
    try:
        return clip(repr(value), MAX_VAR_PREVIEW_CHARS)
    except Exception:  # noqa: BLE001 - a broken repr must not fail the listing
        return f"<repr failed: {type(value).__name__}>"


def user_names(namespace: dict) -> dict:
    """Namespace entries that are user data rather than interpreter plumbing.

    ``/vars`` and the ``state_changed`` flag both compare namespaces, and both
    must ignore the interpreter's own bookkeeping: ``In``/``Out`` history,
    ``_``/``__`` scratch names and injected builtins.
    """
    return {
        name: value
        for name, value in namespace.items()
        if not name.startswith(_PRIVATE_PREFIX) and name not in _IPYTHON_BUILTINS
    }


def namespace_fingerprint(namespace: dict) -> dict:
    """Cheap fingerprint used to report whether a cell mutated the namespace.

    Identity per name detects added, removed and rebound names. In-place
    mutation of an existing object (``df["x"] = 1``) is not detected; callers
    that need that should re-run ``/vars`` explicitly.
    """
    return {name: id(value) for name, value in user_names(namespace).items()}


def list_variables(namespace: dict) -> dict:
    """Summarize user-defined names in an execution namespace.

    Bounded to ``MAX_VARS`` entries; private/dunder names, modules, callables
    and classes are omitted because they are rarely what an agent needs.
    """
    entries: list[dict] = []
    truncated = False
    for name, value in sorted(user_names(namespace).items()):
        if callable(value) or isinstance(value, (type, types.ModuleType)):
            continue
        if len(entries) >= MAX_VARS:
            truncated = True
            break
        shape = getattr(value, "shape", None)
        entries.append(
            {
                "name": name,
                "type": type(value).__name__,
                "kind": _kind(value),
                "shape": json_safe(shape) if shape is not None else None,
                "preview": _preview(value),
            }
        )
    return {"vars": entries, "total": len(entries), "truncated": truncated}
