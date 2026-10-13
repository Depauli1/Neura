"""End-to-end tests for the notebook attach-mode bridge (``neura_bridge``)."""

from __future__ import annotations

import concurrent.futures
import json

import httpx
import pytest

from neura_core.execution import MAX_INSPECT_ROWS

SETUP = (
    "import pandas as pd\n"
    "bridge_df = pd.DataFrame({\n"
    "    'id': [1, 2, 3, 4, 5],\n"
    "    'score': [1.5, None, 3.5, 4.0, 2.0],\n"
    "    'label': ['a', 'b', 'a', 'c', 'b'],\n"
    "})\n"
    "print('kernel bootstrap')\n"
    "21 * 2"
)


def test_attach_handshake_describes_the_bridge(bridge):
    handshake = bridge.handshake
    assert handshake["neura_bridge"] is True
    assert handshake["version"] == "0.1.0"
    assert isinstance(handshake["port"], int) and handshake["port"] > 0
    assert handshake["pid"] > 0
    assert "kernel_name" in handshake
    assert "connection_file" in handshake
    health = bridge.client.get("/health").json()
    assert health["status"] == "ready"
    assert health["busy"] is False


def test_bridge_shares_the_http_surface_with_the_runtime(bridge):
    assert bridge.eval(SETUP).json()["execution"]["result_repr"] == "42"
    assert bridge.eval("bridge_df.shape[0]").json()["execution"]["result_repr"] == "5"
    assert bridge.eval("print('again')").json()["execution"]["stdout"] == "again\n"
    assert any(entry["name"] == "bridge_df" for entry in bridge.vars().json()["vars"])
    inspected = bridge.inspect("bridge_df", 2).json()
    assert inspected["ok"] is True
    assert inspected["inspect"]["shape"] == [5, 3]
    profiled = bridge.profile("bridge_df").json()
    assert profiled["ok"] is True
    assert profiled["profile"]["columns"][0]["name"] == "id"
    failed = bridge.eval("print('partial')\n1 / 0").json()
    assert failed["ok"] is False
    assert failed["error"]["type"] == "ZeroDivisionError"
    assert failed["execution"]["stdout"] == "partial\n"
    assert bridge.last_error().json()["last_error"]["type"] == "ZeroDivisionError"


def test_bridge_cells_share_the_notebook_namespace(bridge):
    # The bridge sees variables defined by the notebook's own cells...
    bridge.run_frontend_cell("notebook_value = 7")
    assert bridge.eval("notebook_value + 1").json()["execution"]["result_repr"] == "8"
    # ...and the notebook sees variables the bridge created.
    bridge.eval("bridge_value = 'shared'")
    assert bridge.run_frontend_cell("print(bridge_value)") == "shared\n"


def test_bridge_requires_the_token(bridge):
    anonymous = httpx.Client(
        base_url=f"http://127.0.0.1:{bridge.port}", timeout=10, trust_env=False
    )
    try:
        assert anonymous.get("/health").status_code == 401
        assert anonymous.post("/eval", json={"code": "1"}).status_code == 401
        assert anonymous.get("/vars").status_code == 401
        assert (
            bridge.client.post(
                "/eval", json={"code": "1"}, headers={"Authorization": "Bearer wrong"}
            ).status_code
            == 401
        )
    finally:
        anonymous.close()


def test_bridge_interrupt_and_timeout(bridge):
    bridge.eval("import time")
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(bridge.eval, "time.sleep(10)", timeout_s=20)
        bridge.wait_until_busy()
        assert bridge.client.post("/interrupt").json()["ok"] is True
        payload = running.result().json()
    assert payload["ok"] is False
    assert payload["error"]["type"] == "KeyboardInterrupt"
    # The kernel is still healthy after the interrupt.
    assert bridge.eval("1 + 1").json()["execution"]["result_repr"] == "2"
    timed_out = bridge.eval("time.sleep(5)", timeout_s=0.3).json()
    assert timed_out["error"]["type"] == "TimeoutError"
    assert bridge.eval("2 + 2").json()["execution"]["result_repr"] == "4"


def test_bridge_serializes_against_the_frontend(bridge):
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(bridge.eval, "import time\ntime.sleep(0.8)\n'busy'", timeout_s=10)
        bridge.wait_until_busy()
        assert bridge.vars().status_code == 409
        assert bridge.eval("'queued'").status_code == 409
        assert bridge.inspect("bridge_df").status_code == 409
        assert running.result().json()["execution"]["result_repr"] == "'busy'"
    assert bridge.eval("'after'").json()["execution"]["result_repr"] == "'after'"


def test_bridge_inspect_is_bounded_and_validated(bridge):
    bridge.eval(SETUP)
    clipped = bridge.inspect("bridge_df", MAX_INSPECT_ROWS).json()["inspect"]
    assert clipped["row_count"] == 5
    assert clipped["truncated"] is False
    missing = bridge.inspect("nope").json()
    assert missing["ok"] is False
    assert missing["error"]["type"] == "DataFrameError"
    not_a_frame = (
        bridge.inspect("notebook_value").json() if bridge.eval("notebook_value = 1") else None
    )
    assert not_a_frame is not None and not_a_frame["ok"] is False
    assert bridge.inspect("bridge_df", MAX_INSPECT_ROWS + 1).status_code == 422
    bad = bridge.client.post("/inspect", content=b"{", headers={"Content-Type": "application/json"})
    assert bad.status_code == 422


def test_bridge_profile_budget_and_partial(bridge):
    bridge.eval(SETUP)
    profile = bridge.profile("bridge_df", {"sample_rows": 2}).json()["profile"]
    # rows_sampled reports a truncated head scan; sample_rows only controls
    # how many example rows ride along with the profile.
    assert profile["rows_sampled"] is False
    assert profile["sample"]["row_count"] == 2
    capped = bridge.profile("bridge_df", {"max_rows": 2, "sample_rows": 2}).json()["profile"]
    assert capped["rows_sampled"] is True
    assert capped["rows_scanned"] == 2
    assert profile["columns"][1]["null_count"] == 1
    bridge.eval(
        "import pandas as pd\n"
        "wide = pd.DataFrame({f'c{i}': [f'v-{i}-{j}' for j in range(2_000)] for i in range(300)})"
    )
    partial = bridge.profile("wide", {"time_budget_s": 0.1}).json()["profile"]
    assert partial["partial"] is True
    json.dumps(partial, allow_nan=False)


def test_bridge_rich_outputs(bridge):
    bridge.eval(SETUP)
    payload = bridge.eval(
        "import matplotlib\n"
        "matplotlib.use('Agg')\n"
        "import matplotlib.pyplot as plt\n"
        "from IPython.display import HTML, display\n"
        "display(HTML('<table><tr><td>bridge</td></tr></table>'))\n"
        "plt.figure(figsize=(2, 2))\n"
        "plt.plot([1, 2, 3])\n"
        "plt.show()\n"
        "bridge_df"
    ).json()
    assert payload["ok"] is True
    kinds = [item["kind"] for item in payload["execution"]["outputs"]]
    assert kinds == ["html", "dataframe", "image_png"]
    image = payload["execution"]["outputs"][2]
    assert image["format"] == "png"
    assert image["data"].startswith("iVBOR")
    assert image["width"] > 0
    json.dumps(payload, allow_nan=False)
    # Figures are closed after capture, so the next cell does not repeat them.
    assert (
        bridge.eval("import matplotlib.pyplot as plt\nlen(plt.get_fignums())").json()["execution"][
            "result_repr"
        ]
        == "0"
    )


def test_bridge_stop_keeps_the_kernel_alive(bridge):
    assert bridge.client.post("/stop").json()["stopped"] is True
    deadline = 10
    with pytest.raises(httpx.HTTPError):
        for _ in range(deadline * 10):
            bridge.client.get("/health", timeout=1)
            import time

            time.sleep(0.1)
    # The kernel itself keeps serving the notebook.
    assert bridge.run_frontend_cell("print('still alive')\n1 + 1") == "still alive\n"


def test_bridge_does_not_publish_into_the_notebook(bridge):
    # Bridge cell output is captured for the API consumer, not for the cell.
    assert bridge.run_frontend_cell("print('frontend only')") == "frontend only\n"
    payload = bridge.eval("print('bridge only')").json()
    assert payload["execution"]["stdout"] == "bridge only\n"
