"""End-to-end tests for rich output capture: stdout, HTML, PNG, DataFrames."""

from __future__ import annotations

import concurrent.futures
import json

from neura_core.execution import (
    MAX_FIGURE_BYTES,
    MAX_FIGURES,
    MAX_OUTPUT_CHARS,
    MAX_RESULT_REPR_CHARS,
    MAX_RICH_HTML_CHARS,
)


def test_plain_stdout_and_result_repr(runtime):
    payload = runtime.eval("print('hello')\nvalue = 'value'\nvalue").json()
    assert payload["ok"] is True
    execution = payload["execution"]
    assert execution["stdout"] == "hello\n"
    assert execution["result_repr"] == "'value'"
    assert execution["result_kind"] == "value"
    assert execution["outputs"] == []
    assert execution["state_changed"] is True
    # A pure expression cell prints but does not mutate the namespace.
    quiet = runtime.eval("print('again')").json()["execution"]
    assert quiet["stdout"] == "again\n"
    assert quiet["result_kind"] == "none"
    assert quiet["state_changed"] is False


def test_stdout_is_clipped_and_flags_truncation(runtime):
    payload = runtime.eval(f"print('x' * ({MAX_OUTPUT_CHARS + 500}))").json()
    stdout = payload["execution"]["stdout"]
    assert len(stdout) == MAX_OUTPUT_CHARS
    assert stdout.endswith("... [truncated]")
    assert payload["execution"]["stdout_truncated"] is True
    assert payload["execution"]["rich_truncated"] is False
    # The captured stream stays bounded: the runtime never buffers it all.
    quiet = runtime.eval("import sys\nsys.stdout.write('y' * 4096)\nNone").json()
    assert quiet["execution"]["stdout"] == "y" * 4096


def test_rich_html_is_captured_and_sanitizable(runtime):
    payload = runtime.eval(
        "from IPython.display import HTML, display\n"
        "display(HTML('<table><tr><th>h</th></tr><tr><td>1</td></tr></table>'))"
    ).json()
    assert payload["ok"] is True
    outputs = payload["execution"]["outputs"]
    assert [item["kind"] for item in outputs] == ["html"]
    html = outputs[0]["html"]
    assert "<table>" in html and "<td>1</td>" in html
    assert outputs[0]["truncated"] is False
    assert payload["execution"]["rich_truncated"] is False
    json.dumps(payload, allow_nan=False)


def test_rich_html_is_clipped_at_the_budget(runtime):
    payload = runtime.eval(
        "from IPython.display import HTML, display\n"
        f"display(HTML('<p>' + 'z' * {MAX_RICH_HTML_CHARS + 2000} + '</p>'))"
    ).json()
    html = payload["execution"]["outputs"][0]
    assert len(html["html"]) == MAX_RICH_HTML_CHARS
    assert html["truncated"] is True
    assert payload["execution"]["rich_truncated"] is True


def test_matplotlib_figures_become_png_items(runtime):
    payload = runtime.eval(
        "import matplotlib\n"
        "matplotlib.use('Agg')\n"
        "import matplotlib.pyplot as plt\n"
        "plt.figure(figsize=(2, 2))\n"
        "plt.plot([1, 2, 3])\n"
        "plt.title('agg')\n"
        "plt.show()"
    ).json()
    assert payload["ok"] is True
    outputs = payload["execution"]["outputs"]
    assert [item["kind"] for item in outputs] == ["image_png"]
    image = outputs[0]
    assert image["format"] == "png"
    assert image["bytes"] <= MAX_FIGURE_BYTES
    assert len(image["data"]) * 3 // 4 <= image["bytes"]
    assert image["data"][:8].startswith("iVBOR")
    assert image["width"] > 0 and image["height"] > 0


def test_extra_figures_are_capped_and_flagged(runtime):
    payload = runtime.eval(
        "import matplotlib\n"
        "matplotlib.use('Agg')\n"
        "import matplotlib.pyplot as plt\n"
        f"for i in range({MAX_FIGURES + 2}):\n"
        "    plt.figure()\n"
        "    plt.plot([1, i])\n"
        "    plt.show()"
    ).json()
    assert payload["ok"] is True
    outputs = payload["execution"]["outputs"]
    assert len([item for item in outputs if item["kind"] == "image_png"]) == MAX_FIGURES
    assert payload["execution"]["rich_truncated"] is True
    figures = runtime.eval("import matplotlib.pyplot as plt\nlen(plt.get_fignums())").json()
    assert figures["execution"]["result_repr"] == "0"


def test_displayed_dataframe_becomes_a_dataframe_item(runtime):
    # A DataFrame cell RESULT is captured as a structured item...
    payload = runtime.eval(
        "import pandas as pd\ndf = pd.DataFrame({'a': range(5), 'b': list('xy' * 3)[:5]})\ndf"
    ).json()
    assert payload["ok"] is True
    outputs = payload["execution"]["outputs"]
    assert [item["kind"] for item in outputs] == ["dataframe"]
    frame = outputs[0]
    assert [column["name"] for column in frame["columns"]] == ["a", "b"]
    assert frame["row_count"] == 5
    assert frame["truncated"] is False
    assert frame["rows"][0] == [0, "x"]
    json.dumps(payload, allow_nan=False)
    # ...while display(df) arrives through the HTML formatter as sanitizable HTML.
    displayed = runtime.eval("from IPython.display import display\ndisplay(df)").json()
    items = displayed["execution"]["outputs"]
    assert [item["kind"] for item in items] == ["html"]
    assert "<table" in items[0]["html"] and "<td>1</td>" in items[0]["html"]
    # The result repr of a DataFrame cell stays the plain repr.
    plain = runtime.eval("df").json()
    assert "a  b" in plain["execution"]["result_repr"]


def test_result_repr_is_clipped_and_broken_reprs_are_reported(runtime):
    long_value = "'x' * 2000"
    payload = runtime.eval(f"value = {long_value}\nvalue").json()
    result_repr = payload["execution"]["result_repr"]
    assert len(result_repr) == MAX_RESULT_REPR_CHARS
    assert result_repr.endswith("... [truncated]")

    class Broken:
        def __repr__(self):
            raise ValueError("no repr")

    broken = runtime.eval(
        "class Broken:\n    def __repr__(self):\n        raise ValueError('no repr')\nBroken()"
    ).json()
    assert broken["ok"] is True
    assert broken["execution"]["result_repr"] == "<repr failed: ValueError>"


def test_rich_payloads_survive_json_round_trip(runtime):
    payload = runtime.eval(
        "import matplotlib\n"
        "matplotlib.use('Agg')\n"
        "import matplotlib.pyplot as plt\n"
        "import numpy as np\n"
        "from IPython.display import HTML, display\n"
        "display(HTML('<b>rich</b>'))\n"
        "plt.figure(); plt.imshow(np.arange(9).reshape(3, 3)); plt.show()\n"
        "np.array([1.0, 2.0])"
    ).json()
    encoded = json.dumps(payload, allow_nan=False)
    decoded = json.loads(encoded)
    kinds = [item["kind"] for item in decoded["execution"]["outputs"]]
    assert kinds == ["html", "image_png"]
    assert len(encoded) > 1000
    assert decoded["execution"]["rich_truncated"] is False


def test_rich_outputs_capture_nothing_for_silent_cells(runtime):
    payload = runtime.eval("x = 1").json()
    assert payload["execution"]["stdout"] == ""
    assert payload["execution"]["result_repr"] is None
    assert payload["execution"]["result_kind"] == "none"
    assert payload["execution"]["state_changed"] is True
    assert payload["execution"]["outputs"] == []
    assert runtime.eval("pass").json()["execution"]["state_changed"] is False


def test_stdout_written_by_c_code_is_captured(runtime):
    payload = runtime.eval("import sys\nsys.stderr.write('warn\\n')\nprint('out')").json()
    assert payload["ok"] is True
    assert payload["execution"]["stdout"] == "out\n"
    assert payload["execution"]["stderr"] == "warn\n"


def test_interrupt_and_timeout_keep_the_stream_bounded(runtime):
    # The cell prints and then signals readiness on disk, so the interrupt
    # below can never race ahead of the print it is meant to follow.
    code = (
        "print('a')\n"
        "import time\n"
        f"open({str(runtime.workspace / 'started')!r}, 'w').write('go')\n"
        "time.sleep(10)"
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(runtime.eval, code, timeout_s=30)
        runtime.wait_for_file("started")
        assert runtime.client.post("/interrupt").json()["ok"] is True
        payload = running.result().json()
    assert payload["ok"] is False
    assert payload["error"]["type"] == "KeyboardInterrupt"
    assert payload["execution"]["stdout"] == "a\n"
    assert runtime.eval("1 + 1").json()["execution"]["result_repr"] == "2"

    timed_out = runtime.eval(
        f"print('b' * {MAX_OUTPUT_CHARS + 10})\nimport time\ntime.sleep(5)", timeout_s=0.2
    ).json()
    assert timed_out["error"]["type"] == "TimeoutError"
    assert len(timed_out["execution"]["stdout"]) == MAX_OUTPUT_CHARS
    assert timed_out["execution"]["stdout_truncated"] is True
