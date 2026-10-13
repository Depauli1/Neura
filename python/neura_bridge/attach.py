"""CLI helper: attach the Neura bridge to a running local Jupyter kernel.

Usage (the VS Code extension spawns exactly this)::

    python -m neura_bridge.attach --workspace <dir> --token <token> \
        [--kernel <connection-file>] [--timeout 30]

The helper discovers kernel connection files in the Jupyter runtime directory,
connects to a live kernel, and injects a one-time bootstrap that starts
:func:`neura_bridge.start_bridge` inside it. The bridge's handshake (port, PID,
version) is printed as a single JSON line on stdout for the extension.

Exit codes:
    0  attached; handshake JSON on stdout
    2  several live kernels; candidate JSON on stdout, re-run with --kernel
    3  jupyter_client is not installed in this interpreter
    4  no live local kernel was found
    5  attaching failed (details on stderr)
    6  bad arguments
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
from pathlib import Path

HANDSHAKE_PREFIX = "NEURA_BRIDGE_HANDSHAKE:"
ERROR_PREFIX = "NEURA_BRIDGE_ERROR:"
PROBE_TIMEOUT_S = 5.0
DEFAULT_TIMEOUT_S = 30.0


class AttachError(RuntimeError):
    """Attaching to the chosen kernel failed."""


def _connection_files() -> list[Path]:
    """Connection files in the Jupyter runtime dir, newest first."""
    try:
        from jupyter_core.paths import jupyter_runtime_dir
    except Exception as exc:  # noqa: BLE001 - jupyter_core comes with jupyter_client
        raise AttachError(f"jupyter_core is unavailable: {exc}") from exc
    directory = Path(jupyter_runtime_dir())
    files = [path for path in directory.glob("kernel-*.json") if path.is_file()]
    return sorted(files, key=lambda path: path.stat().st_mtime, reverse=True)


def _kernel_name(path: Path) -> str | None:
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a partial or foreign file is not ours
        return None
    name = info.get("kernel_name") if isinstance(info, dict) else None
    return name if isinstance(name, str) else None


def _candidate(path: Path) -> dict:
    return {
        "connection_file": str(path),
        "kernel_name": _kernel_name(path),
        "started_ago_s": round(max(0.0, time.time() - path.stat().st_mtime), 1),
    }


def _connect(connection_file: Path):
    """A started-channels client for one connection file (any jupyter_client)."""
    from jupyter_client import BlockingKernelClient

    try:  # jupyter_client < 8.6 exposed this as a classmethod
        loaded = BlockingKernelClient.load_connection_file(str(connection_file))
    except Exception:  # noqa: BLE001 - newer versions make it an instance method
        loaded = None
    if isinstance(loaded, BlockingKernelClient):
        loaded.start_channels()
        return loaded
    client = BlockingKernelClient()
    client.load_connection_file(str(connection_file))
    client.start_channels()
    return client


def _live_kernels(timeout: float = PROBE_TIMEOUT_S) -> list[Path]:
    """Connection files whose kernel answers a kernel_info handshake."""
    live: list[Path] = []
    for path in _connection_files():
        client = None
        try:
            client = _connect(path)
            client.wait_for_ready(timeout=timeout)
            live.append(path)
        except Exception:  # noqa: BLE001 - stale file or dead kernel
            continue
        finally:
            if client is not None:
                with contextlib.suppress(Exception):
                    client.stop_channels()
    return live


def _bootstrap_source(token: str, workspace: Path) -> str:
    """Kernel-side code that starts the bridge and prints its handshake."""
    return (
        "import json\n"
        "try:\n"
        "    from neura_bridge import start_bridge\n"
        "except BaseException as _exc:\n"
        f"    print({ERROR_PREFIX!r} + json.dumps("
        "{{'error': 'import neura_bridge failed: %s: %s' % (type(_exc).__name__, _exc)}}))\n"
        "else:\n"
        "    try:\n"
        f"        _info = start_bridge(token={token!r}, workspace={str(workspace)!r})\n"
        f"        print({HANDSHAKE_PREFIX!r} + json.dumps(_info), flush=True)\n"
        "    except BaseException as _exc:\n"
        f"        print({ERROR_PREFIX!r} + json.dumps("
        "{{'error': '%s: %s' % (type(_exc).__name__, _exc)}}))\n"
    )


def attach(connection_file: Path, token: str, workspace: Path, timeout: float) -> dict:
    """Inject the bridge bootstrap into one kernel and return its handshake."""
    client = _connect(connection_file)
    try:
        try:
            client.wait_for_ready(timeout=PROBE_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - dead or unresponsive kernel
            raise AttachError(f"kernel {connection_file.name} is not responding: {exc}") from exc
        message_id = client.execute(_bootstrap_source(token, workspace))
        handshake: dict | None = None
        failure: str | None = None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = max(0.1, deadline - time.monotonic())
            try:
                message = client.get_iopub_msg(timeout=remaining)
            except Exception:  # noqa: BLE001 - channel closed mid-wait
                break
            if message["parent_header"].get("msg_id") != message_id:
                continue
            kind = message["msg_type"]
            if kind == "stream":
                for line in message["content"].get("text", "").splitlines():
                    line = line.strip()
                    if line.startswith(HANDSHAKE_PREFIX):
                        handshake = json.loads(line[len(HANDSHAKE_PREFIX) :])
                    elif line.startswith(ERROR_PREFIX):
                        failure = json.loads(line[len(ERROR_PREFIX) :]).get("error", "unknown")
            elif kind == "error":
                failure = f"{message['content'].get('ename')}: {message['content'].get('evalue')}"
            elif kind == "status" and message["content"]["execution_state"] == "idle":
                break
        if handshake is None:
            raise AttachError(failure or "the kernel did not report a bridge handshake in time")
        handshake["connection_file"] = str(connection_file)
        handshake["kernel_name"] = _kernel_name(connection_file)
        return handshake
    finally:
        with contextlib.suppress(Exception):
            client.stop_channels()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Attach the Neura bridge to a running local Jupyter kernel."
    )
    parser.add_argument("--workspace", required=True, type=Path, help="workspace directory")
    parser.add_argument("--token", required=True, help="Bearer token for the bridge")
    parser.add_argument(
        "--kernel", default=None, help="connection file path or name; skip discovery"
    )
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="seconds to wait for the handshake"
    )
    args = parser.parse_args(argv)
    workspace = args.workspace.expanduser().resolve()
    if not workspace.is_dir():
        print(
            f"neura_bridge: --workspace must be an existing directory: {workspace}", file=sys.stderr
        )
        return 6
    if not args.token:
        print("neura_bridge: --token must not be empty", file=sys.stderr)
        return 6
    try:
        import jupyter_client  # noqa: F401
    except ImportError:
        print(
            "neura_bridge: jupyter_client is not installed in this interpreter.\n"
            "Install it with: python -m pip install jupyter_client",
            file=sys.stderr,
        )
        return 3

    try:
        if args.kernel:
            chosen = _resolve_kernel(args.kernel)
            if chosen is None:
                print(f"neura_bridge: no such connection file: {args.kernel}", file=sys.stderr)
                return 5
        else:
            live = _live_kernels()
            if not live:
                print(
                    "neura_bridge: no live local Jupyter kernel found.\n"
                    "Open a notebook in VS Code and run a cell first, then attach again.",
                    file=sys.stderr,
                )
                return 4
            if len(live) > 1:
                print(
                    json.dumps({"type": "candidates", "kernels": [_candidate(p) for p in live]}),
                    flush=True,
                )
                return 2
            chosen = live[0]
        handshake = attach(chosen, args.token, workspace, max(1.0, args.timeout))
    except AttachError as exc:
        print(f"neura_bridge: {exc}", file=sys.stderr)
        return 5
    print(json.dumps(handshake), flush=True)
    return 0


def _resolve_kernel(name: str) -> Path | None:
    """Resolve a --kernel value given as a path or a connection file name."""
    candidate = Path(name).expanduser()
    if candidate.is_file():
        return candidate.resolve()
    for path in _connection_files():
        if path.name == name or path.stem == name:
            return path
    return None


if __name__ == "__main__":
    raise SystemExit(main())
