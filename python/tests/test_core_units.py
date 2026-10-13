"""Unit tests for the shared inspection/profiling/rich-output core."""

from __future__ import annotations

import json
import time

import pytest

from neura_core.execution import (
    MAX_FIGURE_BYTES,
    MAX_INSPECT_ROWS,
    MAX_RICH_HTML_CHARS,
    MAX_VARS,
)
from neura_core.frames import (
    DataFrameError,
    frame_preview,
    inspect_dataframe,
    profile_dataframe,
    resolve_dataframe,
)
from neura_core.jsonval import json_safe
from neura_core.rich import (
    DisplayCollector,
    dataframe_item,
    figure_item,
    html_item,
    png_item,
)
from neura_core.streams import BoundedTextIO
from neura_core.variables import list_variables

pd = pytest.importorskip("pandas")


def frame():
    return pd.DataFrame(
        {
            "id": [1, 2, 3, 4, 5],
            "score": [1.5, None, 3.5, 4.0, 2.0],
            "label": ["a", "b", "a", "c", "b"],
        }
    )


def test_json_safe_normalizes_runtime_values():
    import datetime as dt

    import numpy as np

    assert json_safe(np.int64(3)) == 3
    assert json_safe(np.float64(1.5)) == 1.5
    assert json_safe(np.bool_(True)) is True
    assert json_safe(float("nan")) is None
    assert json_safe(float("inf")) is None
    assert json_safe(dt.datetime(2026, 10, 13, 12, 0)) == "2026-10-13T12:00:00"
    assert json_safe(dt.timedelta(seconds=2)) == 2.0
    assert json_safe(b"abc") == "<bytes 3 bytes>"
    assert json_safe({1, 2}) == [1, 2]
    assert json_safe(np.array([1, 2])) == [1, 2]

    class Broken:
        def __repr__(self):
            raise ValueError("no repr")

    assert json_safe(Broken()) == "<repr failed: ValueError>"
    long = "x" * 5000
    assert json_safe(long, 100).endswith("... [truncated]")
    assert len(json_safe(long, 100)) == 100


def test_bounded_stream_keeps_prefix_and_reports_truncation():
    stream = BoundedTextIO(limit=10)
    assert stream.write("hello world, this is long") == len("hello world, this is long")
    assert stream.getvalue() == "hello... [truncated]"
    assert stream.truncated is True
    assert stream.truncated is True
    assert BoundedTextIO(limit=10).write("short") == 5


def test_variable_listing_filters_and_bounds():
    namespace = {
        "df": frame(),
        "value": 41,
        "_private": 1,
        "__dunder__": 2,
        "func": lambda: None,
        "Klass": type("Klass", (), {}),
        "module": time,
        "text": "x" * 500,
    }
    listed = list_variables(namespace)
    names = [entry["name"] for entry in listed["vars"]]
    assert names == ["df", "text", "value"]
    kinds = {entry["name"]: entry["kind"] for entry in listed["vars"]}
    assert kinds["df"] == "dataframe"
    assert len(listed["vars"][1]["preview"]) == 200
    assert listed["truncated"] is False

    many = {f"v{i}": i for i in range(MAX_VARS + 25)}
    assert list_variables(many)["truncated"] is True
    assert len(list_variables(many)["vars"]) == MAX_VARS


def test_inspect_dataframe_preview_and_errors():
    namespace = {"df": frame(), "number": 7}
    preview = inspect_dataframe(namespace, "df", 2)
    assert preview["shape"] == [5, 3]
    assert preview["row_count"] == 2
    assert preview["more_rows"] is True
    assert [column["name"] for column in preview["columns"]] == ["id", "score", "label"]
    assert preview["columns"][1]["dtype"].startswith("float")
    # NaN becomes JSON null, never a non-serializable float.
    assert preview["rows"][0][1] is None or preview["rows"][0][1] == 1.5
    assert inspect_dataframe(namespace, "df", 0)["rows"] == []
    wide = pd.DataFrame({"a": range(MAX_INSPECT_ROWS + 5)})
    clipped = inspect_dataframe({"wide": wide}, "wide", MAX_INSPECT_ROWS + 5)
    assert clipped["row_count"] == MAX_INSPECT_ROWS
    assert clipped["truncated"] is True

    for name, exc_type in (("missing", DataFrameError), ("number", DataFrameError)):
        with pytest.raises(exc_type):
            inspect_dataframe(namespace, name)
    with pytest.raises(DataFrameError):
        resolve_dataframe(namespace, "not an identifier")
    with pytest.raises(DataFrameError):
        frame_preview(object(), 5)


def test_profile_covers_dtypes_nulls_cardinality_and_samples():
    namespace = {"df": frame()}
    profile = profile_dataframe(namespace, "df")
    assert profile["shape"] == [5, 3]
    assert profile["rows_sampled"] is False
    assert profile["partial"] is False
    columns = {column["name"]: column for column in profile["columns"]}
    assert columns["score"]["null_count"] == 1
    assert columns["score"]["null_pct"] == 0.2
    assert columns["id"]["cardinality"] == 5
    assert columns["id"]["numeric"]["min"] == 1
    assert set(columns["id"]["numeric"]["quantiles"]) == {"0.25", "0.5", "0.75"}
    assert columns["label"]["categorical"]["top"][0] == {"value": "a", "count": 2}
    assert columns["label"]["categorical"]["other_count"] == 0
    assert profile["sample"]["row_count"] == 5
    assert profile["payload_bytes"] <= 512 * 1024
    # Every value survives a strict JSON round-trip.
    json.dumps(profile, allow_nan=False)


def test_profile_config_is_clamped_and_rows_are_capped():
    big = pd.DataFrame({"a": range(10_000), "b": ["x"] * 10_000})
    profile = profile_dataframe({"big": big}, "big", {"max_rows": 5, "sample_rows": 3})
    assert profile["rows_sampled"] is True
    assert profile["rows_scanned"] == 5
    assert profile["sample"]["row_count"] == 3
    clamped = profile_dataframe(
        {"big": big}, "big", {"max_rows": 10**12, "top_k": 10**6, "sample_rows": -3}
    )
    assert clamped["rows_scanned"] == 10_000
    assert clamped["sample"] is None  # sample_rows clamped to 0


def test_profile_returns_partial_when_the_deadline_hits():
    # 40 object columns of 20k unique values: profiling every column takes
    # longer than the configured budget, so later columns come back partial.
    frame_data = pd.DataFrame(
        {f"c{i}": [f"value-{i}-{j}" for j in range(20_000)] for i in range(40)}
    )
    profile = profile_dataframe({"wide": frame_data}, "wide", {"time_budget_s": 0.05})
    assert profile["partial"] is True
    partial_columns = [c for c in profile["columns"] if c.get("partial")]
    assert partial_columns, "expected at least one partial column"
    assert profile["sample"] is None


def test_profile_shrinks_oversized_payloads():
    huge = "y" * 120_000
    frame_data = pd.DataFrame({"a": [huge, huge, "z"], "b": [huge, "q", huge]})
    profile = profile_dataframe({"huge": frame_data}, "huge")
    assert profile["payload_bytes"] <= 512 * 1024
    json.dumps(profile, allow_nan=False)


def test_rich_helpers_apply_budgets():
    html = "<table>" + "x" * (MAX_RICH_HTML_CHARS + 10) + "</table>"
    item = html_item(html)
    assert item["truncated"] is True
    assert len(item["html"]) == MAX_RICH_HTML_CHARS
    small = png_item(b"png-bytes")
    assert small["kind"] == "image_png"
    assert small["data"] == "cG5nLWJ5dGVz"
    assert small["bytes"] == 9
    assert small["truncated"] is False
    assert (small["width"], small["height"]) == (0, 0)
    assert png_item("not base64!!") is None
    oversized = png_item(b"x" * (MAX_FIGURE_BYTES + 1))
    assert oversized["kind"] == "text" and "budget" in oversized["text"]


def test_display_collector_routes_mimebundles():
    items: list[dict] = []
    stdout = BoundedTextIO()
    collector = DisplayCollector(items, stdout)
    assert collector.is_publishing is False
    collector.publish({"text/html": "<b>hi</b>"})
    collector.publish({"image/png": b"abc"})
    collector.publish({"text/plain": "plain text"})
    collector.publish("not a bundle")
    collector.clear_output()
    assert [item["kind"] for item in items] == ["html", "image_png"]
    assert stdout.getvalue() == "plain text"


def test_dataframe_and_figure_items_for_cell_results():
    assert dataframe_item(frame())["kind"] == "dataframe"
    assert dataframe_item(42) is None

    pytest.importorskip("matplotlib")
    from matplotlib.figure import Figure

    figure = Figure()
    figure.add_subplot().plot([1, 2], [3, 4])
    item = figure_item(figure)
    assert item is not None and item["kind"] == "image_png" and item["bytes"] > 0
    assert figure_item(42) is None
