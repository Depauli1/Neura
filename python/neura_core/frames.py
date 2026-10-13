"""pandas DataFrame inspection and budgeted profiling (V1: pandas-first).

Both the managed runtime and the notebook bridge expose these through
``POST /inspect`` and ``POST /profile``. Everything here is bounded: rows
scanned, sample size, top-k categories, per-value size, and a wall-clock
budget after which the profile is returned marked ``partial``.
"""

from __future__ import annotations

import time
from typing import Any

from neura_core.execution import (
    MAX_INSPECT_ROWS,
    MAX_INSPECT_VALUE_CHARS,
    MAX_PROFILE_PAYLOAD_BYTES,
    MAX_PROFILE_ROWS,
    MAX_PROFILE_SAMPLE_ROWS,
    MAX_PROFILE_TOP_K,
    PROFILE_TIME_BUDGET_S,
    clip,
)
from neura_core.jsonval import json_safe

DEFAULT_QUANTILES = (0.25, 0.5, 0.75)
# value_counts() is skipped above this cardinality to keep profiles cheap.
MAX_TOPK_CARDINALITY = 1000


class DataFrameError(ValueError):
    """The requested variable is missing or is not a DataFrame."""


def _pandas():
    try:
        import pandas as pd
    except Exception as exc:  # noqa: BLE001 - pandas is a hard dependency, but
        raise DataFrameError(f"pandas is not available: {exc}") from exc  # be safe
    return pd


def _dataframe(value: Any) -> Any:
    pd = _pandas()
    if not isinstance(value, pd.DataFrame):
        raise DataFrameError(f"expected a pandas DataFrame, got {type(value).__name__}")
    return value


def resolve_dataframe(namespace: dict, name: str) -> Any:
    """Look up a DataFrame variable, raising :class:`DataFrameError`."""
    if not name or not name.isidentifier():
        raise DataFrameError(f"{name!r} is not a valid variable name")
    if name not in namespace:
        raise DataFrameError(f"no variable named {name!r} in the runtime namespace")
    return _dataframe(namespace[name])


def _columns(df: Any) -> list[dict]:
    return [{"name": str(name), "dtype": str(df.dtypes[name])} for name in df.columns]


def _frame_rows(df: Any, limit: int) -> tuple[list[list], bool]:
    """Return up to ``limit`` rows as JSON-safe lists plus a clipped flag."""
    rows: list[list] = []
    clipped = False
    head = df.head(limit)
    for row in head.itertuples(index=False, name=None):
        rows.append([json_safe(value, MAX_INSPECT_VALUE_CHARS) for value in row])
        if len(rows) >= limit:
            clipped = len(df) > limit
            break
    return rows, clipped


def frame_preview(df: Any, limit: int) -> dict:
    """Structured, JSON-safe preview of a DataFrame object (no namespace)."""
    _dataframe(df)
    rows, clipped = _frame_rows(df, max(0, min(limit, MAX_INSPECT_ROWS)))
    return {
        "shape": [int(df.shape[0]), int(df.shape[1])],
        "columns": _columns(df),
        "rows": rows,
        "row_count": len(rows),
        "truncated": clipped,
    }


def inspect_dataframe(namespace: dict, name: str, n: int = 5) -> dict:
    """Small preview: shape, dtypes, and the first ``n`` rows (bounded)."""
    df = resolve_dataframe(namespace, name)
    preview = frame_preview(df, n)
    null_counts = {}
    try:
        nulls = df.isna().sum()
        null_counts = {str(c): int(nulls[c]) for c in df.columns}
    except Exception:  # noqa: BLE001 - dtypes that cannot answer isna()
        pass
    return {
        "name": name,
        "type": type(df).__name__,
        "shape": preview["shape"],
        "columns": [
            {**column, "null_count": null_counts.get(column["name"])}
            for column in preview["columns"]
        ],
        "rows": preview["rows"],
        "row_count": preview["row_count"],
        "more_rows": preview["shape"][0] > preview["row_count"],
        "truncated": preview["truncated"],
    }


def _numeric_summary(series: Any, quantiles: list[float]) -> dict | None:
    try:
        summary = {
            "min": json_safe(series.min(), MAX_INSPECT_VALUE_CHARS),
            "max": json_safe(series.max(), MAX_INSPECT_VALUE_CHARS),
            "mean": json_safe(series.mean(), MAX_INSPECT_VALUE_CHARS),
            "std": json_safe(series.std(), MAX_INSPECT_VALUE_CHARS),
        }
        if quantiles:
            computed = series.quantile(quantiles)
            summary["quantiles"] = {
                str(q): json_safe(computed.loc[q], MAX_INSPECT_VALUE_CHARS) for q in quantiles
            }
        return summary
    except Exception:  # noqa: BLE001 - exotic numeric dtypes
        return None


def _categorical_topk(series: Any, top_k: int) -> dict | None:
    try:
        counts = series.value_counts(dropna=False)
    except Exception:  # noqa: BLE001 - unhashable or otherwise exotic values
        return None
    total_unique = int(counts.shape[0])
    if total_unique > MAX_TOPK_CARDINALITY:
        return {"unique_capped": True, "cardinality": total_unique, "top": []}
    top = [
        {"value": json_safe(index, MAX_INSPECT_VALUE_CHARS), "count": int(count)}
        for index, count in counts.head(top_k).items()
    ]
    return {
        "top": top,
        "unique_capped": False,
        "cardinality": total_unique,
        "other_count": max(0, total_unique - len(top)),
    }


def _profile_column(series: Any, top_k: int, quantiles: list[float]) -> dict:
    name = str(series.name)
    dtype = str(series.dtype)
    try:
        null_count = int(series.isna().sum())
        cardinality = int(series.nunique(dropna=True))
    except Exception:  # noqa: BLE001
        null_count, cardinality = -1, -1
    column: dict = {
        "name": name,
        "dtype": dtype,
        "null_count": null_count,
        "null_pct": round(null_count / max(1, int(series.shape[0])), 6),
        "cardinality": cardinality,
    }
    kind = getattr(series.dtype, "kind", None)
    if kind in ("i", "u", "f", "c"):
        numeric = _numeric_summary(series, quantiles)
        if numeric is not None:
            column["numeric"] = numeric
    elif kind == "M":
        column["datetime"] = {
            "min": json_safe(series.min(), MAX_INSPECT_VALUE_CHARS),
            "max": json_safe(series.max(), MAX_INSPECT_VALUE_CHARS),
        }
    elif kind == "m":
        column["timedelta"] = {
            "min": json_safe(series.min(), MAX_INSPECT_VALUE_CHARS),
            "max": json_safe(series.max(), MAX_INSPECT_VALUE_CHARS),
        }
    else:
        categorical = _categorical_topk(series, top_k)
        if categorical is not None:
            column["categorical"] = categorical
    return column


def _config(raw: dict | None) -> dict:
    config = {
        "max_rows": MAX_PROFILE_ROWS,
        "sample_rows": 5,
        "top_k": 10,
        "time_budget_s": PROFILE_TIME_BUDGET_S,
        "quantiles": list(DEFAULT_QUANTILES),
    }
    for key, value in (raw or {}).items():
        if key not in config:
            continue
        if key == "quantiles" and isinstance(value, list):
            quantiles = [float(q) for q in value if isinstance(q, (int, float)) and 0 <= q <= 1]
            config[key] = sorted(set(quantiles))[:5]
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            config[key] = value
    config["max_rows"] = int(min(max(config["max_rows"], 1), MAX_PROFILE_ROWS))
    config["sample_rows"] = int(min(max(config["sample_rows"], 0), MAX_PROFILE_SAMPLE_ROWS))
    config["top_k"] = int(min(max(config["top_k"], 1), MAX_PROFILE_TOP_K))
    config["time_budget_s"] = float(min(max(config["time_budget_s"], 0.1), 60.0))
    return config


def profile_dataframe(
    namespace: dict,
    name: str,
    config: dict | None = None,
    deadline: float | None = None,
) -> dict:
    """Budgeted profile: shape, dtypes, nulls, cardinality, summaries, samples.

    ``deadline`` is a ``time.monotonic()`` timestamp; columns that cannot be
    computed before it are returned as partial and ``partial`` is set on the
    profile. When the frame is larger than ``max_rows`` only the head is
    scanned and ``rows_sampled`` is set.
    """
    df = resolve_dataframe(namespace, name)
    settings = _config(config)
    started = time.monotonic()
    # The configured budget is the primary knob; an outer request deadline can
    # only make it stricter, never looser.
    budget_deadline = started + settings["time_budget_s"]
    if deadline is None:
        deadline = budget_deadline
    else:
        deadline = min(deadline, budget_deadline)

    total_rows = int(df.shape[0])
    scanned = df if total_rows <= settings["max_rows"] else df.head(settings["max_rows"])
    rows_sampled = total_rows > settings["max_rows"]

    columns: list[dict] = []
    partial = False
    for column_name in scanned.columns:
        if time.monotonic() >= deadline:
            partial = True
            columns.append(
                {
                    "name": str(column_name),
                    "dtype": str(scanned.dtypes[column_name]),
                    "partial": True,
                }
            )
            continue
        try:
            columns.append(
                _profile_column(scanned[column_name], settings["top_k"], settings["quantiles"])
            )
        except Exception:  # noqa: BLE001 - never fail the whole profile
            partial = True
            columns.append(
                {
                    "name": str(column_name),
                    "dtype": str(scanned.dtypes[column_name]),
                    "partial": True,
                }
            )

    sample: dict | None = None
    if settings["sample_rows"] > 0 and not partial:
        rows, clipped = _frame_rows(scanned, settings["sample_rows"])
        sample = {
            "columns": _columns(scanned),
            "rows": rows,
            "row_count": len(rows),
            "truncated": clipped,
        }

    profile = {
        "name": name,
        "type": type(df).__name__,
        "shape": [total_rows, int(df.shape[1])],
        "rows_scanned": int(scanned.shape[0]),
        "rows_sampled": rows_sampled,
        "columns": columns,
        "partial": partial,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "sample": sample,
    }
    return _shrink(profile)


def _shrink(profile: dict) -> dict:
    """Drop the heaviest optional sections until the payload fits the budget."""
    import json

    for attempt in range(4):
        try:
            size = len(json.dumps(profile, default=str).encode("utf-8"))
        except Exception:  # noqa: BLE001 - non-serializable leftovers
            profile = {**profile, "columns": profile.get("columns", [])[:0]}
            continue
        if size <= MAX_PROFILE_PAYLOAD_BYTES:
            profile["payload_bytes"] = size
            return profile
        if profile.get("sample") is not None:
            profile["sample"] = None
        elif any(
            isinstance(column.get("categorical"), dict) and column["categorical"].get("top")
            for column in profile.get("columns", [])
        ):
            for column in profile["columns"]:
                categorical = column.get("categorical")
                if isinstance(categorical, dict):
                    categorical["top"] = categorical.get("top", [])[:1]
        else:
            profile["columns"] = profile.get("columns", [])[:20]
            profile["partial"] = True
            if attempt == 3:
                break
    profile["payload_truncated"] = True
    return profile


__all__ = [
    "DataFrameError",
    "clip",
    "frame_preview",
    "inspect_dataframe",
    "profile_dataframe",
    "resolve_dataframe",
]
