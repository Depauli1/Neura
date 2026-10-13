"""Subprocess fixtures: test the real CLI, handshake, socket, and HTTP API."""

from __future__ import annotations

import json
import os
import queue
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest


class RuntimeProcess:
    def __init__(self, workspace: Path, profile_dir: Path, port: int = 0) -> None:
        self.workspace = workspace
        self.profile_dir = profile_dir
        self.token = "-" + secrets.token_urlsafe(32)  # Exercise leading-dash tokens too.
        self.stdout_lines: list[str] = []
        self.stderr_lines: list[str] = []
        self._lines: queue.Queue[str | None] = queue.Queue()
        self.client: httpx.Client | None = None
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "neura_runtime",
                "--port",
                str(port),
                f"--token={self.token}",  # URL-safe tokens can start with '-'.
                "--workspace",
                str(workspace),
            ],
            cwd=workspace,
            env={**os.environ, "PYTHONUNBUFFERED": "1", "IPYTHONDIR": str(profile_dir)},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )

        def read_stdout():
            for line in self.process.stdout:
                self.stdout_lines.append(line)
                self._lines.put(line)
            self._lines.put(None)

        def read_stderr():
            self.stderr_lines.extend(self.process.stderr)

        self._readers = [
            threading.Thread(target=read_stdout, daemon=True),
            threading.Thread(target=read_stderr, daemon=True),
        ]
        for reader in self._readers:
            reader.start()
        try:
            line = self._lines.get(timeout=20)
            if line is None:
                raise AssertionError(f"runtime exited before handshake: {self.logs}")
            self.handshake = json.loads(line)
            self.port = self.handshake["port"]
            self.client = httpx.Client(
                base_url=f"http://127.0.0.1:{self.port}",
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=10,
                trust_env=False,
            )
            # No retry: the handshake itself promises the HTTP server is ready.
            response = self.client.get("/health")
            assert response.status_code == 200, self.logs
            assert response.json()["status"] == "ready", self.logs
        except BaseException:
            self.stop()
            raise

    @property
    def logs(self) -> str:
        return "".join(self.stderr_lines)

    def eval(self, code: str, *, timeout_s: float = 5, cwd: str | None = None):
        return self.client.post("/eval", json={"code": code, "timeout_s": timeout_s, "cwd": cwd})

    def vars(self):
        return self.client.get("/vars")

    def inspect(self, name: str, n: int = 5, **payload):
        return self.client.post("/inspect", json={"name": name, "n": n, **payload})

    def profile(self, name: str, config: dict | None = None, **payload):
        return self.client.post("/profile", json={"name": name, "config": config, **payload})

    def last_error(self):
        return self.client.get("/last_error")

    def wait_until_busy(self) -> None:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if self.client.get("/health").json()["busy"]:
                return
            time.sleep(0.01)
        raise AssertionError(f"runtime never became busy: {self.logs}")

    def wait_for_file(self, name: str) -> None:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if (self.workspace / name).exists():
                return
            time.sleep(0.01)
        raise AssertionError(f"cell did not create {name}: {self.logs}")

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        for reader in self._readers:
            reader.join(timeout=2)
        if self.client is not None:
            self.client.close()
        self.process.stdout.close()
        self.process.stderr.close()


class BridgeProcess:
    """A real IPython kernel plus the attach-mode bridge started inside it.

    Mirrors the VS Code flow: a kernel with its connection file in the Jupyter
    runtime directory, then ``python -m neura_bridge.attach`` to inject the
    bridge and read the handshake from stdout.
    """

    def __init__(self, workspace: Path, profile_dir: Path) -> None:
        jupyter_client = pytest.importorskip("jupyter_client")
        jupyter_core = pytest.importorskip("jupyter_core")
        self.workspace = workspace
        self.profile_dir = profile_dir
        self.token = "-" + secrets.token_urlsafe(32)
        self.stderr_lines: list[str] = []
        runtime_dir = Path(jupyter_core.paths.jupyter_runtime_dir())
        runtime_dir.mkdir(parents=True, exist_ok=True)
        self.connection_file = runtime_dir / f"kernel-{secrets.token_hex(6)}.json"
        self.manager = jupyter_client.KernelManager(
            kernel_name="python3", connection_file=str(self.connection_file)
        )
        self.manager.start_kernel()
        self.kernel_client = self.manager.client()
        self.kernel_client.start_channels()
        self.kernel_client.wait_for_ready(60)
        attach = subprocess.run(
            [
                sys.executable,
                "-m",
                "neura_bridge.attach",
                "--workspace",
                str(workspace),
                f"--token={self.token}",
                "--timeout",
                "60",
            ],
            cwd=workspace,
            env={**os.environ, "PYTHONUNBUFFERED": "1", "IPYTHONDIR": str(profile_dir)},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=180,
        )
        if attach.returncode != 0:
            self.stop_kernel()
            raise AssertionError(f"attach failed: {attach.stdout}\n{attach.stderr}")
        try:
            self.handshake = json.loads(attach.stdout.strip())
        except json.JSONDecodeError as exc:
            self.stop_kernel()
            raise AssertionError(f"no handshake: {attach.stdout}\n{attach.stderr}") from exc
        self.port = self.handshake["port"]
        self.client = httpx.Client(
            base_url=f"http://127.0.0.1:{self.port}",
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=30,
            trust_env=False,
        )
        response = self.client.get("/health")
        assert response.status_code == 200, attach.stderr

    @property
    def logs(self) -> str:
        return "".join(self.stderr_lines)

    def eval(self, code: str, *, timeout_s: float = 10, cwd: str | None = None):
        return self.client.post("/eval", json={"code": code, "timeout_s": timeout_s, "cwd": cwd})

    def vars(self):
        return self.client.get("/vars")

    def inspect(self, name: str, n: int = 5, **payload):
        return self.client.post("/inspect", json={"name": name, "n": n, **payload})

    def profile(self, name: str, config: dict | None = None, **payload):
        return self.client.post("/profile", json={"name": name, "config": config, **payload})

    def last_error(self):
        return self.client.get("/last_error")

    def wait_until_busy(self) -> None:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if self.client.get("/health").json()["busy"]:
                return
            time.sleep(0.01)
        raise AssertionError("bridge never became busy")

    def run_frontend_cell(self, code: str) -> str:
        """Execute a cell through the kernel's own channels (the notebook UI)."""
        msg_id = self.kernel_client.execute(code)
        collected: list[str] = []
        while True:
            message = self.kernel_client.get_iopub_msg(timeout=60)
            if message["parent_header"].get("msg_id") != msg_id:
                continue
            if message["msg_type"] == "stream":
                collected.append(message["content"]["text"])
            elif message["msg_type"] == "error":
                collected.append("\n".join(message["content"]["traceback"]))
            elif (
                message["msg_type"] == "status" and message["content"]["execution_state"] == "idle"
            ):
                return "".join(collected)

    def stop_kernel(self) -> None:
        try:
            self.kernel_client.stop_channels()
        except Exception:  # noqa: BLE001 - teardown must not mask the failure
            pass
        try:
            self.manager.shutdown_kernel(now=True)
        except Exception:  # noqa: BLE001
            pass

    def stop(self) -> None:
        if getattr(self, "client", None) is not None:
            try:
                self.client.post("/stop", timeout=30)
            except Exception:  # noqa: BLE001 - already stopped
                pass
            self.client.close()
        self.stop_kernel()


@pytest.fixture
def bridge_factory(tmp_path):
    bridges: list[BridgeProcess] = []

    def spawn():
        root = tmp_path / str(len(bridges))
        workspace = root / "workspace with spaces"
        workspace.mkdir(parents=True)
        profile = root / "user-ipython"
        profile.mkdir()
        bridge = BridgeProcess(workspace, profile)
        bridges.append(bridge)
        return bridge

    try:
        yield spawn
    finally:
        for bridge in bridges:
            bridge.stop()


@pytest.fixture
def bridge(bridge_factory):
    return bridge_factory()


@pytest.fixture
def runtime_factory(tmp_path):
    processes: list[RuntimeProcess] = []

    def spawn(*, port: int = 0):
        root = tmp_path / str(len(processes))
        workspace = root / "workspace with spaces"
        workspace.mkdir(parents=True)
        profile = root / "user-ipython"
        profile.mkdir()
        (profile / "sentinel.txt").write_text("untouched", encoding="utf-8")
        process = RuntimeProcess(workspace, profile, port=port)
        processes.append(process)
        return process

    try:
        yield spawn
    finally:
        for process in processes:
            process.stop()


@pytest.fixture
def runtime(runtime_factory):
    return runtime_factory()
