"""In-kernel HTTP bridge for ``.ipynb`` attach mode (local kernels only).

The VS Code extension injects :func:`start_bridge` into a running local Jupyter
kernel (see :mod:`neura_bridge.attach`). The bridge serves the same
authenticated, budgeted HTTP contract as ``neura_runtime`` from a background
thread inside the kernel, so the extension can evaluate code, list variables,
and inspect/profile DataFrames in the notebook's live namespace.

Nothing here is imported at package import time beyond the standard library and
``neura_core``; IPython/uvicorn are imported lazily so the package stays cheap
to import from the attach helper.
"""

from __future__ import annotations

import asyncio
import os
import threading
from typing import Any

from neura_core.api import API_VERSION

__version__ = "0.1.0"

HOST = "127.0.0.1"
STARTUP_TIMEOUT_S = 15.0

_bridge_lock = threading.Lock()
_state: dict[str, Any] = {}


class BridgeError(RuntimeError):
    """The bridge could not start (already running, no kernel, bad token)."""


def _kernel_shell() -> Any:
    try:
        from IPython import get_ipython
    except Exception as exc:  # noqa: BLE001 - IPython is required in-kernel
        raise BridgeError(f"IPython is not available: {exc}") from exc
    shell = get_ipython()
    if shell is None:
        raise BridgeError("neura_bridge must run inside an IPython kernel (no active shell)")
    return shell


def _kernel_event_loop() -> asyncio.AbstractEventLoop | None:
    """The kernel's running loop, or None when there is none to reuse."""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def start_bridge(
    *,
    token: str,
    workspace: str | None = None,
    host: str = HOST,
    port: int = 0,
) -> dict:
    """Start the in-kernel bridge once per kernel and return its handshake.

    Idempotent: a second call with the same token returns the existing
    handshake; a different token is refused. The HTTP server runs on a daemon
    thread, so it never outlives the kernel.
    """
    if not token:
        raise BridgeError("a non-empty token is required")
    with _bridge_lock:
        existing = _state.get("handshake")
        if existing is not None:
            if _state.get("token") != token:
                raise BridgeError(
                    "a Neura bridge is already running in this kernel with a "
                    "different token; stop it first"
                )
            return dict(existing)
        shell = _kernel_shell()
        loop = _kernel_event_loop()

        from neura_bridge.kernel import KernelExecutor
        from neura_core.http import create_app

        executor = KernelExecutor(shell, loop, workspace=workspace, on_stop=_stop_server)

        import uvicorn

        config = uvicorn.Config(
            create_app(token, executor),
            host=host,
            port=port,
            access_log=False,
            log_level="warning",
            loop="asyncio",
            proxy_headers=False,
            timeout_graceful_shutdown=3,
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, name="neura-bridge-http", daemon=True)
        thread.start()
        deadline = STARTUP_TIMEOUT_S
        while not server.started and thread.is_alive() and deadline > 0:
            thread.join(timeout=0.05)
            deadline -= 0.05
        if not server.started:
            server.should_exit = True
            thread.join(timeout=2)
            raise BridgeError("bridge HTTP server failed to start")

        handshake = {
            "neura_bridge": True,
            "port": _bound_port(server),
            "pid": os.getpid(),
            "version": API_VERSION,
            "kernel": type(shell).__name__,
        }
        _state.update(
            {
                "token": token,
                "handshake": handshake,
                "server": server,
                "thread": thread,
                "executor": executor,
            }
        )
        return dict(handshake)


def _bound_port(server: Any) -> int:
    for asyncio_server in getattr(server, "servers", []) or []:
        for sock in getattr(asyncio_server, "sockets", []) or []:
            address = sock.getsockname()
            if isinstance(address, tuple) and len(address) >= 2:
                return int(address[1])
    return 0


def _stop_server() -> None:
    """Stop the HTTP listener; safe to call from a request handler thread."""
    server = _state.get("server")
    if server is not None:
        server.should_exit = True


def stop_bridge() -> bool:
    """Stop the bridge's HTTP server, leaving the kernel and namespace intact."""
    with _bridge_lock:
        if not _state:
            return False
        executor = _state.get("executor")
        server = _state.get("server")
        thread = _state.get("thread")
        _state.clear()
    if executor is not None:
        executor.stop()
    if server is not None:
        server.should_exit = True
    if thread is not None and threading.current_thread() is not thread:
        thread.join(timeout=5)
    return True


def bridge_running() -> bool:
    return bool(_state.get("handshake"))


def load_ipython_extension(ipython: Any) -> None:
    """``%load_ext neura_bridge`` entry point (manual/debug use).

    Reads the token from ``NEURA_BRIDGE_TOKEN`` when present and prints the
    handshake JSON so a human can point the extension at the bridge.
    """
    token = os.environ.get("NEURA_BRIDGE_TOKEN", "").strip()
    if not token:
        import secrets

        token = "-" + secrets.token_urlsafe(32)
    handshake = start_bridge(token=token, workspace=os.getcwd())
    import json

    print("NEURA_BRIDGE_HANDSHAKE:" + json.dumps(handshake), flush=True)


__all__ = [
    "BridgeError",
    "bridge_running",
    "load_ipython_extension",
    "start_bridge",
    "stop_bridge",
]
