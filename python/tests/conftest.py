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
