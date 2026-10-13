"""End-to-end tests for the inspection surface: /vars, /inspect, /profile."""

from __future__ import annotations

import concurrent.futures
import json

import httpx
import pytest

from neura_core.execution import MAX_INSPECT_ROWS, MAX_PROFILE_PAYLOAD_BYTES, MAX_VARS

SETUP = """
import pandas as pd
import numpy as np

df = pd.DataFrame({
    "id": [1, 2, 3, 4, 5],
    "score": [1.5, None, 3.5, 4.0, 2.0],
    "label": ["a", "b", "a", "c", "b"],
    "when": pd.to_datetime(["2026-01-0%d" % (i + 1) for i in range(5)]),
})
series = pd.Series([1, 2, 3])
number = 41
text = "hello"
def func():
    return 1
class Klass:
    pass
import math as _mod
"""


def test_vars_lists_user_variables_with_kinds(runtime):
    payload = runtime.eval(SETUP).json()
    assert payload["ok"] is True
    listed = runtime.vars().json()
    assert listed["ok"] is True
    assert listed["version"] == "0.1.0"
    by_name = {entry["name"]: entry for entry in listed["vars"]}
    assert set(by_name) == {"df", "series", "number", "text"}
    assert by_name["df"]["kind"] == "dataframe"
    assert by_name["df"]["type"] == "DataFrame"
    assert by_name["df"]["shape"] == [5, 4]
    assert by_name["series"]["kind"] == "series"
    assert by_name["number"]["kind"] == "value"
    assert len(by_name["text"]["preview"]) <= 200
    # Private names, callables, classes and modules are omitted.
    assert listed["total"] == 4
    assert listed["truncated"] is False


def test_vars_is_bounded(runtime):
    assert runtime.eval("\n".join(f"v{i} = {i}" for i in range(MAX_VARS + 30))).json()["ok"]
    listed = runtime.vars().json()
    assert listed["total"] == MAX_VARS
    assert listed["truncated"] is True


def test_inspect_returns_shape_dtypes_rows_and_nulls(runtime):
    runtime.eval(SETUP)
    payload = runtime.inspect("df", 2).json()
    assert payload["ok"] is True
    frame = payload["inspect"]
    assert frame["name"] == "df"
    assert frame["type"] == "DataFrame"
    assert frame["shape"] == [5, 4]
    assert [column["name"] for column in frame["columns"]] == ["id", "score", "label", "when"]
    assert frame["columns"][1]["null_count"] == 1
    assert frame["row_count"] == 2
    assert frame["more_rows"] is True
    # NaN is JSON null, timestamps are ISO strings.
    assert frame["rows"][0][1] == 1.5
    assert frame["rows"][1][1] is None
    assert frame["rows"][0][3] == "2026-01-01T00:00:00"
    json.dumps(payload, allow_nan=False)
    assert runtime.inspect("df", 0).json()["inspect"]["rows"] == []
    wide = runtime.eval("wide = __import__('pandas').DataFrame({'a': range(150)})").json()
    assert wide["ok"]
    clipped = runtime.inspect("wide", MAX_INSPECT_ROWS).json()["inspect"]
    assert clipped["row_count"] == MAX_INSPECT_ROWS
    assert clipped["truncated"] is True
    over_limit = runtime.inspect("wide", MAX_INSPECT_ROWS + 1)
    assert over_limit.status_code == 422


def test_inspect_domain_errors_use_the_ok_false_envelope(runtime):
    runtime.eval(SETUP)
    missing = runtime.inspect("nope").json()
    assert missing["ok"] is False
    assert missing["error"]["type"] == "DataFrameError"
    assert "nope" in missing["error"]["message"]
    not_a_frame = runtime.inspect("number").json()
    assert not_a_frame["ok"] is False
    assert not_a_frame["error"]["type"] == "DataFrameError"
    for name in ("", "not an identifier", "x" * 300):
        invalid = runtime.inspect(name).json()
        assert invalid["ok"] is False
    bad = runtime.client.post(
        "/inspect", content=b"{", headers={"Content-Type": "application/json"}
    )
    assert bad.status_code == 422
    for payload in ({"name": "df", "n": -1}, {"name": "df", "unknown": 1}, {"name": 5}):
        response = runtime.client.post("/inspect", json=payload)
        assert response.status_code == 422, payload


def test_profile_reports_dtypes_nulls_cardinality_and_samples(runtime):
    runtime.eval(SETUP)
    payload = runtime.profile("df").json()
    assert payload["ok"] is True
    profile = payload["profile"]
    assert profile["shape"] == [5, 4]
    assert profile["rows_scanned"] == 5
    assert profile["rows_sampled"] is False
    assert profile["partial"] is False
    columns = {column["name"]: column for column in profile["columns"]}
    assert columns["score"]["null_count"] == 1
    assert columns["score"]["null_pct"] == 0.2
    assert columns["id"]["cardinality"] == 5
    assert columns["id"]["numeric"]["min"] == 1
    assert columns["id"]["numeric"]["max"] == 5
    assert set(columns["id"]["numeric"]["quantiles"]) == {"0.25", "0.5", "0.75"}
    assert columns["label"]["categorical"]["top"] == [
        {"value": "a", "count": 2},
        {"value": "b", "count": 2},
        {"value": "c", "count": 1},
    ]
    assert columns["label"]["categorical"]["other_count"] == 0
    assert columns["when"]["datetime"]["min"].startswith("2026-01-01")
    assert profile["sample"]["row_count"] == 5
    assert profile["payload_bytes"] <= MAX_PROFILE_PAYLOAD_BYTES
    json.dumps(payload, allow_nan=False)


def test_profile_config_controls_caps_and_sampling(runtime):
    runtime.eval(
        "import pandas as pd\nbig = pd.DataFrame({'a': range(10_000), 'b': ['x', 'y'] * 5_000})"
    )
    sampled = runtime.profile("big", {"max_rows": 5, "sample_rows": 3}).json()["profile"]
    assert sampled["rows_sampled"] is True
    assert sampled["rows_scanned"] == 5
    assert sampled["sample"]["row_count"] == 3
    clamped = runtime.profile(
        "big", {"max_rows": 10**12, "top_k": 10**6, "sample_rows": -3}
    ).json()["profile"]
    assert clamped["rows_scanned"] == 10_000
    assert clamped["sample"] is None
    top_one = runtime.profile("big", {"top_k": 1}).json()["profile"]
    assert top_one["columns"][1]["categorical"]["top"] == [{"value": "x", "count": 5000}]
    assert top_one["columns"][1]["categorical"]["other_count"] == 1


def test_profile_is_partial_when_the_budget_expires(runtime):
    runtime.eval(
        "import pandas as pd\n"
        "wide = pd.DataFrame({f'c{i}': [f'v-{i}-{j}' for j in range(2_000)] for i in range(300)})"
    )
    profile = runtime.profile("wide", {"time_budget_s": 0.1}).json()["profile"]
    assert profile["partial"] is True
    assert [column for column in profile["columns"] if column.get("partial")]
    assert profile["sample"] is None
    assert runtime.eval("42").json()["execution"]["result_repr"] == "42"


def test_profile_shrinks_oversized_payloads(runtime):
    runtime.eval(
        "import pandas as pd\n"
        "huge = 'y' * 120_000\n"
        "huge_frame = pd.DataFrame({'a': [huge, huge, 'z'], 'b': [huge, 'q', huge]})"
    )
    response = runtime.profile("huge_frame")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["profile"]["payload_bytes"] <= MAX_PROFILE_PAYLOAD_BYTES
    json.dumps(payload, allow_nan=False)


def test_inspection_is_serialized_against_eval(runtime):
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(runtime.eval, "import time\ntime.sleep(0.8)\n'busy'", timeout_s=5)
        runtime.wait_until_busy()
        response = runtime.inspect("df")
        assert response.status_code == 409
        assert response.json()["error"]["type"] == "BusyError"
        assert runtime.vars().status_code == 409
        assert runtime.profile("df").status_code == 409
        assert running.result().json()["execution"]["result_repr"] == "'busy'"
    assert runtime.inspect("df", 1).status_code == 200


def test_last_error_tracks_the_most_recent_failure(runtime):
    assert runtime.last_error().json() == {
        "ok": True,
        "version": "0.1.0",
        "last_error": None,
    }
    payload = runtime.eval("print('before')\nraise ValueError('boom')").json()
    assert payload["ok"] is False
    last = runtime.last_error().json()["last_error"]
    assert last["type"] == "ValueError"
    assert "boom" in last["message"]
    assert "ValueError" in last["traceback"]
    assert runtime.eval("1 + 1").json()["ok"] is True
    assert runtime.last_error().json()["last_error"] == last
    timed_out = runtime.eval("import time\ntime.sleep(5)", timeout_s=0.15).json()
    assert timed_out["error"]["type"] == "TimeoutError"
    assert runtime.last_error().json()["last_error"]["type"] == "TimeoutError"


def test_stop_endpoint_shuts_the_runtime_down(runtime):
    assert runtime.client.post("/stop").json() == {
        "ok": True,
        "version": "0.1.0",
        "stopped": True,
    }
    assert runtime.process.wait(timeout=10) == 0
    with pytest.raises(httpx.HTTPError):
        runtime.client.get("/health")
