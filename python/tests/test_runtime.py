"""End-to-end tests against a spawned runtime, not an in-process TestClient."""

from __future__ import annotations

import ast
import concurrent.futures
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time

import httpx
import pytest

from neura_runtime.executor import MAX_OUTPUT_CHARS, MAX_RESULT_REPR_CHARS
from neura_runtime.server import MAX_REQUEST_BYTES


def test_handshake_ready_loopback_and_clean_stdout(runtime):
    assert runtime.handshake == {
        "neura_runtime": True,
        "port": runtime.port,
        "pid": runtime.process.pid,
        "version": "0.1.0",
    }
    assert 0 < runtime.port <= 65535
    response = runtime.client.get("/health")
    assert response.json() == {"ok": True, "version": "0.1.0", "status": "ready", "busy": False}
    assert response.headers["content-type"] == "application/json; charset=utf-8"
    # Where a non-loopback interface is available, it must not accept this port.
    try:
        addresses = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except socket.gaierror:
        addresses = []
    for address in {item[4][0] for item in addresses}:
        if not address.startswith("127."):
            with pytest.raises(OSError):
                with socket.create_connection((address, runtime.port), timeout=0.5):
                    pytest.fail("runtime is listening on an external interface")
    runtime.eval("print('captured, not printed on the protocol pipe')")
    runtime.stop()
    assert len(runtime.stdout_lines) == 1
    assert runtime.token not in "".join(runtime.stdout_lines) + runtime.logs
    if os.name != "nt":
        assert runtime.process.returncode == 0


def test_bearer_auth_on_every_path_before_body_validation(runtime):
    with httpx.Client(base_url=runtime.client.base_url, timeout=5, trust_env=False) as client:
        for authorization in (None, "Bearer wrong", "Basic anything", "Bearer"):
            headers = {"Authorization": authorization} if authorization else {}
            for method, path in (
                ("GET", "/health"),
                ("POST", "/eval"),
                ("POST", "/interrupt"),
                ("GET", "/openapi.json"),
                ("GET", "/docs"),
                ("GET", "/missing"),
            ):
                response = client.request(method, path, headers=headers, content=b"not json")
                assert response.status_code == 401
                assert response.headers["www-authenticate"] == "Bearer"
                assert response.headers["content-type"] == "application/json; charset=utf-8"
                assert response.json()["ok"] is False
                assert response.json()["error"]["type"] == "Unauthorized"
        assert (
            client.get("/health", headers={"Authorization": f"bearer {runtime.token}"}).status_code
            == 200
        )
    for path in ("/openapi.json", "/docs", "/missing"):
        response = runtime.client.get(path)
        assert response.status_code == 404
        assert response.json()["ok"] is False


def test_persistent_namespace_stdout_stderr_and_last_expression(runtime):
    response = runtime.eval(
        "import sys\nvalue = 40\nprint('hello 🌍')\nprint('warning', file=sys.stderr)\nvalue + 2"
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["version"] == "0.1.0"
    execution = payload["execution"]
    assert execution["stdout"] == "hello 🌍\n"
    assert execution["stderr"] == "warning\n"
    assert execution["result_repr"] == "42"
    assert execution["duration_ms"] >= 0
    assert execution["stdout_truncated"] is False
    assert execution["stderr_truncated"] is False
    assert runtime.eval("value += 1\nvalue").json()["execution"]["result_repr"] == "41"
    assert runtime.eval("_ + 1").json()["execution"]["result_repr"] == "42"
    assert runtime.eval("None").json()["execution"]["result_repr"] is None
    assert runtime.eval("123;").json()["execution"]["result_repr"] is None
    assert runtime.eval("").json()["ok"] is True


def test_non_utf8_user_output_is_json_escaped(runtime):
    payload = runtime.eval(r"print('\ud800')").json()
    assert payload["ok"] is True
    assert payload["execution"]["stdout"] == "\ud800\n"
    payload = runtime.eval(r"raise ValueError('\udfff')").json()
    assert payload["ok"] is False
    assert payload["error"]["message"] == "\udfff"
    assert runtime.eval("42").json()["execution"]["result_repr"] == "42"


def test_ipython_magics_and_local_imports(runtime):
    (runtime.workspace / "local_helper.py").write_text("answer = 23\n", encoding="utf-8")
    result = runtime.eval("import local_helper\nlocal_helper.answer").json()
    assert result["execution"]["result_repr"] == "23"
    result = runtime.eval("%pwd").json()
    assert result["ok"] is True
    assert ast.literal_eval(result["execution"]["result_repr"]) == str(runtime.workspace)
    assert [path.name for path in runtime.profile_dir.iterdir()] == ["sentinel.txt"]
    assert (runtime.profile_dir / "sentinel.txt").read_text(encoding="utf-8") == "untouched"


@pytest.mark.parametrize(
    ("code", "error_type", "source"),
    [
        ("print('before error')\n1 / 0", "ZeroDivisionError", "1 / 0"),
        ("def broken(:", "SyntaxError", "def broken(:"),
        ("raise ValueError('bad input')", "ValueError", "bad input"),
        ("raise SystemExit(7)", "SystemExit", "SystemExit"),
    ],
)
def test_structured_errors_do_not_kill_the_shell(runtime, code, error_type, source):
    response = runtime.eval(code)
    assert response.status_code == 200  # execution failures, not transport failures
    payload = response.json()
    assert payload["ok"] is False
    assert payload["error"]["type"] == error_type
    assert source in payload["error"]["traceback"]
    assert "\x1b[" not in payload["error"]["traceback"]
    assert "Traceback" not in payload["execution"]["stdout"]
    if error_type == "ZeroDivisionError":
        assert payload["execution"]["stdout"] == "before error\n"
    assert runtime.eval("2 + 3").json()["execution"]["result_repr"] == "5"
    assert runtime.client.get("/health").json()["busy"] is False


def test_repr_failure_is_not_an_execution_failure(runtime):
    payload = runtime.eval(
        "class BadRepr:\n    def __repr__(self):\n        raise ValueError('no repr')\nBadRepr()"
    ).json()
    assert payload["ok"] is True
    assert payload["execution"]["result_repr"] == "<repr failed: ValueError>"


def test_working_directory_is_per_eval_and_restored(runtime):
    alternate = runtime.workspace / "subdirectory"
    alternate.mkdir()
    (runtime.workspace / "data.txt").write_text("root", encoding="utf-8")
    (alternate / "data.txt").write_text("alternate", encoding="utf-8")
    assert runtime.eval("open('data.txt').read()").json()["execution"]["result_repr"] == "'root'"
    for cwd in (str(alternate), "subdirectory"):
        payload = runtime.eval("open('data.txt').read()", cwd=cwd).json()
        assert payload["execution"]["result_repr"] == "'alternate'"
    # Restore even if user code itself changed cwd, or raised while in it.
    runtime.eval("import os\nos.chdir('subdirectory')")
    payload = runtime.eval("open('data.txt').read()").json()
    assert payload["execution"]["result_repr"] == "'root'"
    assert runtime.eval("1 / 0", cwd=str(alternate)).json()["ok"] is False
    assert runtime.eval("open('data.txt').read()").json()["execution"]["result_repr"] == "'root'"
    payload = runtime.eval("pass", cwd="does-not-exist").json()
    assert payload["ok"] is False
    assert payload["error"]["type"] == "FileNotFoundError"
    assert runtime.eval("123").json()["ok"] is True


def test_busy_rejects_second_eval_but_health_and_interrupt_stay_responsive(runtime):
    sleep_s = 0.8 if os.name == "nt" else 10
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(
            runtime.eval,
            "import time\nfrom pathlib import Path\nbefore = 1\nprint('started')\n"
            f"Path('started.flag').touch()\ntime.sleep({sleep_s})\nafter = 1",
        )
        runtime.wait_until_busy()
        runtime.wait_for_file("started.flag")
        assert runtime.client.get("/health").json()["status"] == "busy"
        started = time.monotonic()
        response = runtime.eval("rejected_cell = True")
        assert time.monotonic() - started < 1
        assert response.status_code == 409
        assert response.json()["error"]["type"] == "BusyError"
        assert response.json()["version"] == "0.1.0"
        response = runtime.client.post("/interrupt")
        assert response.json() == {"ok": True, "version": "0.1.0", "interrupted": True}
        payload = running.result(timeout=8).json()
        assert payload["ok"] is False
        assert payload["error"]["type"] == "KeyboardInterrupt"
        assert payload["execution"]["stdout"] == "started\n"
    result = runtime.eval("(before, 'after' in globals(), 'rejected_cell' in globals())").json()
    assert result["execution"]["result_repr"] == "(1, False, False)"
    assert runtime.client.post("/interrupt").json()["interrupted"] is False
    assert runtime.eval("42").json()["ok"] is True  # idle interrupt must be a no-op


@pytest.mark.parametrize("code", ["import time\ntime.sleep(10)", "while True:\n    pass"])
def test_timeout_interrupts_and_runtime_recovers(runtime, code):
    if os.name == "nt":
        # Windows only delivers the simulated interrupt once native sleep returns.
        code = code.replace("sleep(10)", "sleep(0.6)")
    started = time.monotonic()
    payload = runtime.eval("print('partial')\n" + code, timeout_s=0.15).json()
    assert time.monotonic() - started < 3
    assert payload["ok"] is False
    assert payload["error"]["type"] == "TimeoutError"
    assert "timeout_s=0.15" in payload["error"]["message"]
    assert payload["execution"]["stdout"] == "partial\n"
    assert runtime.client.get("/health").json()["busy"] is False
    assert runtime.eval("'still alive'").json()["execution"]["result_repr"] == "'still alive'"


def test_timeout_is_reported_even_if_user_catches_interrupt(runtime):
    payload = runtime.eval(
        "import time\ntry:\n    time.sleep(0.6)\nexcept KeyboardInterrupt:\n    print('caught')",
        timeout_s=0.1,
    ).json()
    assert payload["ok"] is False
    assert payload["error"]["type"] == "TimeoutError"
    assert payload["execution"]["stdout"] == "caught\n"
    assert runtime.eval("pass").json()["ok"] is True


def test_interrupted_cell_cancels_its_timer_not_the_next_cell(runtime):
    sleep_s = 0.2 if os.name == "nt" else 10
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(runtime.eval, f"import time\ntime.sleep({sleep_s})", timeout_s=0.4)
        runtime.wait_until_busy()
        runtime.client.post("/interrupt")
        assert running.result(timeout=5).json()["error"]["type"] == "KeyboardInterrupt"
    # The old deadline falls during this cell; it must not interrupt this one.
    payload = runtime.eval("import time\ntime.sleep(0.6)\n99", timeout_s=2).json()
    assert payload["ok"] is True
    assert payload["execution"]["result_repr"] == "99"
    # Also check a normally completed cell's timer is cancelled.
    assert runtime.eval("1", timeout_s=0.2).json()["ok"] is True
    assert runtime.eval("time.sleep(0.3)\n2", timeout_s=2).json()["ok"] is True


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX interruption of blocking sleep")
def test_best_effort_timeout_keeps_busy_until_execution_really_stops(runtime):
    payload = runtime.eval(
        "import time\ntry:\n    time.sleep(10)\nexcept KeyboardInterrupt:\n    time.sleep(4)",
        timeout_s=0.1,
    ).json()
    assert payload["ok"] is False
    assert payload["error"]["type"] == "TimeoutError"
    assert payload["busy"] is True
    assert runtime.client.get("/health").json()["busy"] is True
    assert runtime.eval("should_not_run = 1").status_code == 409
    runtime.client.post("/interrupt")
    deadline = time.monotonic() + 5
    while runtime.client.get("/health").json()["busy"] and time.monotonic() < deadline:
        time.sleep(0.02)
    assert (
        runtime.eval("'should_not_run' in globals()").json()["execution"]["result_repr"] == "False"
    )


def test_output_and_result_are_bounded(runtime):
    payload = runtime.eval(
        f"import sys\nprint('x' * {MAX_OUTPUT_CHARS + 100})\n"
        f"print('y' * {MAX_OUTPUT_CHARS + 100}, file=sys.stderr)\n'z' * 5000"
    ).json()
    assert payload["ok"] is True
    execution = payload["execution"]
    assert execution["stdout"] == "x" * MAX_OUTPUT_CHARS
    assert execution["stderr"] == "y" * MAX_OUTPUT_CHARS
    assert execution["stdout_truncated"] is True
    assert execution["stderr_truncated"] is True
    assert len(execution["result_repr"]) <= MAX_RESULT_REPR_CHARS
    assert execution["result_repr_truncated"] is True
    # Recent IPython must not retain a second, unbounded copy of these streams.
    assert (
        runtime.eval("len(getattr(get_ipython().history_manager, 'outputs', {}))").json()[
            "execution"
        ]["result_repr"]
        == "0"
    )


def test_request_validation_and_payload_limits(runtime):
    for payload in (
        {},
        {"code": 123},
        {"code": "pass", "unknown": True},
        *(
            {"code": "pass", "timeout_s": value}
            for value in (-1, 0, 3601, None, True, "one", float("nan"), float("inf"))
        ),
        {"code": "pass", "cwd": 123},
    ):
        # httpx's json encoder disallows NaN/Infinity; send them explicitly to
        # exercise the server, whose error response must still be valid JSON.
        response = runtime.client.post(
            "/eval", content=json.dumps(payload), headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 422, (payload, response.text)
        assert response.json()["error"]["type"] == "ValidationError"
    assert (
        runtime.client.post(
            "/eval", content=b"{", headers={"Content-Type": "application/json"}
        ).status_code
        == 422
    )
    assert (
        runtime.client.post(
            "/eval", content=b"{}", headers={"Content-Type": "text/plain"}
        ).status_code
        == 415
    )
    response = runtime.client.post(
        "/eval",
        content=b" " * (MAX_REQUEST_BYTES + 1),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json()["error"]["type"] == "PayloadTooLarge"
    # The limit must also work with chunked requests (no Content-Length).
    response = runtime.client.post(
        "/eval",
        content=iter([b" " * (MAX_REQUEST_BYTES // 2)] * 3),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert runtime.client.post("/eval", json={"code": "1 + 1"}).json()["ok"] is True
    assert runtime.client.get("/health").json()["busy"] is False


def test_simultaneous_eval_requests_have_one_winner(runtime):
    barrier = threading.Barrier(2)

    def run(label):
        barrier.wait(timeout=3)
        return runtime.eval(f"import time\ntime.sleep(0.4)\nwinner = {label!r}\nwinner")

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, label) for label in ("first", "second")]
        responses = [future.result(timeout=5) for future in futures]
    assert sorted(response.status_code for response in responses) == [200, 409]
    accepted = next(response.json() for response in responses if response.status_code == 200)
    assert accepted["ok"] is True
    assert (
        runtime.eval("winner").json()["execution"]["result_repr"]
        == accepted["execution"]["result_repr"]
    )


def test_very_small_deadlines_do_not_kill_the_executor(runtime):
    for _ in range(5):
        payload = runtime.eval("while True:\n    pass", timeout_s=0.005).json()
        assert payload["ok"] is False
        assert payload["error"]["type"] == "TimeoutError"
        assert runtime.eval("42").json()["execution"]["result_repr"] == "42"


@pytest.mark.skipif(os.name == "nt", reason="requires graceful POSIX SIGTERM")
def test_sigterm_during_eval_finishes_request_and_exits(runtime):
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(
            runtime.eval,
            "import time\nfrom pathlib import Path\nPath('running.flag').touch()\ntime.sleep(30)",
        )
        runtime.wait_for_file("running.flag")
        runtime.process.terminate()
        payload = running.result(timeout=5).json()
        assert payload["ok"] is False
        assert payload["error"]["type"] == "KeyboardInterrupt"
        assert runtime.process.wait(timeout=5) == 0


@pytest.mark.skipif(os.name == "nt", reason="Windows process termination is not graceful SIGINT")
def test_sigint_shutdown_releases_the_port(runtime):
    runtime.process.send_signal(signal.SIGINT)
    assert runtime.process.wait(timeout=5) == 0
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", runtime.port), timeout=0.5)


def _run_cli(workspace, *args):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "neura_runtime",
            "--token",
            "test-token",
            "--workspace",
            str(workspace),
            *args,
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_cli_explicit_port_is_honored(runtime_factory):
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    runtime = runtime_factory(port=port)
    assert runtime.port == port
    assert runtime.eval("6 * 7").json()["execution"]["result_repr"] == "42"


def test_cli_invalid_options_fail_without_handshake(tmp_path):
    for args in (("--port", "-1"), ("--port", "65536"), ("--token", ""), ("--host", "0.0.0.0")):
        result = _run_cli(tmp_path, *args)
        assert result.returncode != 0
        assert result.stdout == ""
    for workspace in (tmp_path / "missing", tmp_path / "file.txt"):
        if workspace.name == "file.txt":
            workspace.write_text("not a directory", encoding="utf-8")
        result = _run_cli(workspace)
        assert result.returncode != 0
        assert result.stdout == ""
        assert "existing directory" in result.stderr
    result = subprocess.run(
        [sys.executable, "-m", "neura_runtime", "--workspace", str(tmp_path)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert "--token" in result.stderr


def test_cli_occupied_port_fails_without_handshake(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        result = _run_cli(tmp_path, "--port", str(listener.getsockname()[1]))
    assert result.returncode != 0
    assert result.stdout == ""
    assert "neura_runtime:" in result.stderr
