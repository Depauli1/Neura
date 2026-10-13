"""Kernel-side executor for ``.ipynb`` attach mode.

The bridge's HTTP server runs on worker threads inside the Jupyter kernel
process, but user code must run where the kernel itself would run it: on the
event loop thread (the process main thread). Jobs are therefore submitted with
``asyncio.run_coroutine_threadsafe`` onto the kernel's loop, which serializes
them with any cell the frontend executes.

Interrupts exploit the same fact: while a bridge job runs, a private SIGINT
handler is installed on the main thread that raises ``KeyboardInterrupt``, so
``POST /interrupt`` and timeouts behave like the managed runtime (IPython
captures the interrupt into the cell's structured error). Outside a bridge job
the kernel's own signal disposition is untouched, so the notebook's Interrupt
button keeps working.

Limitations (documented in docs/runtime-development.md):
- A bridge cell blocks the kernel's event loop while it runs, exactly like a
  normal notebook cell; frontend control messages wait for it to finish.
- Cells using top-level ``await`` are executed synchronously and are not
  supported in attach mode.
- Outputs of bridge-executed code are captured for the Neura Outputs panel
  instead of being published into the notebook.
"""

from __future__ import annotations

import _thread
import asyncio
import concurrent.futures
import contextlib
import os
import signal
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from neura_core.api import error, ok
from neura_core.execution import (
    DEFAULT_INSPECT_TIMEOUT_S,
    DEFAULT_TIMEOUT_S,
    INTERRUPT_GRACE_S,
    MAX_MESSAGE_CHARS,
    MAX_RESULT_REPR_CHARS,
    MAX_TRACEBACK_CHARS,
    BusyError,
    RuntimeStoppingError,
    clip,
)
from neura_core.execution import (
    Executor as BaseExecutor,
)
from neura_core.frames import inspect_dataframe, profile_dataframe
from neura_core.rich import (
    DisplayCollector,
    QuietResultHook,
    capture_new_figures,
    dataframe_item,
    figure_item,
    figure_numbers,
)
from neura_core.streams import BoundedTextIO
from neura_core.variables import list_variables


@dataclass
class _Job:
    kind: str
    code: str = ""
    timeout_s: float = DEFAULT_TIMEOUT_S
    cwd: str | None = None
    name: str = ""
    n: int = 5
    config: dict | None = None
    timed_out: bool = False
    interrupt_requested: bool = False


def _noop(*args: Any, **kwargs: Any) -> None:
    return None


@contextlib.contextmanager
def _null_tee(channel: Any):
    """Stand-in for IPython 9's ``InteractiveShell._tee`` (no history capture)."""
    yield


class _QuietOutputs:
    """Suppress the kernel's notebook publishing while a bridge job runs.

    IPython publishes tracebacks, display data and execution results to the
    notebook frontend. A bridge cell has no frontend parent message, so those
    publications would be dropped or misattached; the bridge captures the same
    information structurally for the Neura Outputs panel instead. IPython 9's
    stream tee is disabled too, so captured text is not also duplicated into
    unbounded output history.
    """

    def __init__(self, shell: Any, items: list[dict], stdout: Any) -> None:
        self.shell = shell
        self.items = items
        self.stdout = stdout
        self._saved: list[tuple[str, bool, Any]] = []
        self._trap: Any = None

    def __enter__(self) -> _QuietOutputs:
        for name in ("display_pub", "displayhook", "showtraceback", "showsyntaxerror", "_tee"):
            had_own = name in vars(self.shell)
            self._saved.append((name, had_own, vars(self.shell).get(name)))
        self.shell.display_pub = DisplayCollector(self.items, self.stdout)
        self.shell.displayhook = QuietResultHook(shell=self.shell)
        self.shell.showtraceback = _noop
        self.shell.showsyntaxerror = _noop
        self.shell._tee = _null_tee
        # run_cell() enters shell.display_trap, which installs the trap's own
        # hook as sys.displayhook; that is the object the trailing expression
        # actually reaches, so it is the one that must be swapped.
        self._trap = getattr(self.shell, "display_trap", None)
        if self._trap is not None:
            self._saved.append(("__trap_hook__", True, self._trap.hook))
            self._trap.hook = self.shell.displayhook
        return self

    def __exit__(self, *exc_info: Any) -> None:
        if self._trap is not None:
            self._trap.hook = self._saved.pop()[2]
            self._trap = None
        for name, had_own, original in reversed(self._saved):
            if had_own:
                setattr(self.shell, name, original)
            else:
                with contextlib.suppress(AttributeError):
                    delattr(self.shell, name)
        self._saved.clear()


class KernelExecutor(BaseExecutor):
    """Executor bound to a live IPython kernel's shell and event loop."""

    def __init__(
        self,
        shell: Any,
        loop: asyncio.AbstractEventLoop | None = None,
        workspace: str | None = None,
        on_stop: Callable[[], None] | None = None,
    ) -> None:
        self._shell = shell
        self._loop = loop
        self.workspace = os.path.abspath(workspace or os.getcwd())
        self._on_stop = on_stop
        self._lock = threading.Lock()
        self._active: _Job | None = None
        self._armed: _Job | None = None  # job whose SIGINT handler is installed
        self._stopping = False
        self._last_error: dict | None = None
        self._previous_sigint: Any = None
        # Fallback for hosts without a running event loop (plain IPython).
        self._pool: concurrent.futures.ThreadPoolExecutor | None = None
        if loop is None:
            self._pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="neura-bridge"
            )

    # ------------------------------------------------------------------ state

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._active is not None

    @property
    def shell(self) -> Any:
        return self._shell

    # ------------------------------------------------------------- submission

    def _submit(self, kind: str, timeout_s: float, **fields) -> dict:
        """Reserve the slot immediately; never queue behind another job."""
        job = _Job(kind=kind, timeout_s=timeout_s, **fields)
        with self._lock:
            if self._stopping:
                raise RuntimeStoppingError("bridge is shutting down")
            if self._active is not None:
                raise BusyError("kernel is busy executing another request")
            self._active = job
        try:
            if self._loop is not None:
                future = asyncio.run_coroutine_threadsafe(self._run(job), self._loop)
            else:
                future = self._pool.submit(self._run, job)  # type: ignore[union-attr]
        except RuntimeError as exc:  # loop closed / pool shut down
            with self._lock:
                self._active = None
            raise RuntimeStoppingError(f"kernel execution thread is unavailable: {exc}") from exc
        try:
            return future.result(timeout=timeout_s + INTERRUPT_GRACE_S)
        except TimeoutError:
            self._request_interrupt(job, timed_out=True)
            # The slot stays reserved until the job actually finishes.
            return error(
                "TimeoutError",
                f"execution exceeded timeout_s={timeout_s}; "
                "interrupt requested but execution is still running",
                busy=True,
            )
        except concurrent.futures.CancelledError as exc:
            raise RuntimeStoppingError("kernel execution was cancelled") from exc

    async def _run(self, job: _Job) -> dict:
        """Coroutine body; runs on the kernel's event loop (main) thread."""
        try:
            return self._execute(job)
        finally:
            with self._lock:
                if self._active is job:
                    self._active = None
                if self._armed is job:
                    self._armed = None

    def eval(self, code: str, timeout_s: float = DEFAULT_TIMEOUT_S, cwd: str | None = None) -> dict:
        return self._submit("eval", timeout_s, code=code, cwd=cwd)

    def list_vars(self, timeout_s: float = DEFAULT_INSPECT_TIMEOUT_S) -> dict:
        return self._submit("vars", timeout_s)

    def inspect_df(
        self, name: str, n: int = 5, timeout_s: float = DEFAULT_INSPECT_TIMEOUT_S
    ) -> dict:
        return self._submit("inspect", timeout_s, name=name, n=n)

    def profile_df(
        self,
        name: str,
        config: dict | None = None,
        timeout_s: float = DEFAULT_INSPECT_TIMEOUT_S,
    ) -> dict:
        return self._submit("profile", timeout_s, name=name, config=config)

    def last_error(self) -> dict:
        return self._submit("last_error", DEFAULT_INSPECT_TIMEOUT_S)

    def interrupt(self) -> bool:
        with self._lock:
            job = self._active
        if job is None:
            return False
        return self._request_interrupt(job)

    def stop(self) -> None:
        with self._lock:
            self._stopping = True
        self.interrupt()
        if self._on_stop is not None:
            self._on_stop()

    # -------------------------------------------------------------- interrupts

    def _request_interrupt(self, job: _Job, *, timed_out: bool = False) -> bool:
        with self._lock:
            if self._active is not job:
                return False
            if timed_out and job.timed_out:
                return False  # one timeout notification per job
            job.interrupt_requested = True
            job.timed_out |= timed_out
            armed = self._armed is job
        if armed:
            self._signal()
        return True

    @staticmethod
    def _signal() -> None:
        """Ask the main thread to raise KeyboardInterrupt right now."""
        if os.name == "nt":
            _thread.interrupt_main()
        else:
            with contextlib.suppress(OSError):
                os.kill(os.getpid(), signal.SIGINT)

    def _install_sigint(self, job: _Job) -> None:
        """Raise KeyboardInterrupt on SIGINT while this job runs (main thread).

        Only ever called from the kernel's main thread. Any failure leaves the
        kernel's original disposition in place: interrupts then fall back to
        the notebook's own best-effort behavior.
        """
        if threading.current_thread() is not threading.main_thread():
            return

        def handler(signum, frame):  # noqa: ANN001 - signal handler signature
            raise KeyboardInterrupt

        try:
            self._previous_sigint = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, handler)
        except (ValueError, OSError):  # pragma: no cover - non-main-thread edge
            self._previous_sigint = None
            return
        with self._lock:
            self._armed = job

    def _restore_sigint(self, job: _Job) -> None:
        with self._lock:
            if self._armed is not job:
                return
            self._armed = None
        if threading.current_thread() is not threading.main_thread():
            return
        previous = self._previous_sigint
        self._previous_sigint = None
        if previous is None:
            return
        with contextlib.suppress(ValueError, OSError, TypeError):
            signal.signal(signal.SIGINT, previous)

    # -------------------------------------------------------------- execution

    @staticmethod
    def _error_fields(exc: BaseException) -> dict:
        return {
            "type": type(exc).__name__,
            "message": clip(str(exc) or type(exc).__name__, MAX_MESSAGE_CHARS),
            "traceback": clip(
                "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
                MAX_TRACEBACK_CHARS,
            ),
        }

    def _execute(self, job: _Job) -> dict:
        if job.kind == "eval":
            return self._execute_eval(job)
        return self._execute_readonly(job)

    def _execute_eval(self, job: _Job) -> dict:
        started = time.monotonic()
        stdout, stderr = BoundedTextIO(), BoundedTextIO()
        previous_cwd = os.getcwd()
        err = None
        result_repr = None
        result_truncated = False
        items: list[dict] = []
        before_figures = figure_numbers()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                try:
                    self._install_sigint(job)
                    if job.interrupt_requested or self._stopping:
                        raise KeyboardInterrupt
                    cwd = os.path.join(self.workspace, job.cwd) if job.cwd else self.workspace
                    os.chdir(cwd)
                    with _QuietOutputs(self._shell, items, stdout):
                        # store_history=False: bridge cells must not disturb the
                        # notebook's execution counters or In/Out history.
                        res = self._shell.run_cell(job.code, store_history=False)
                    exc = res.error_before_exec or res.error_in_exec
                    if exc is not None:
                        err = self._error_fields(exc)
                    else:
                        if res.result is not None:
                            try:
                                rendered = repr(res.result)
                            except Exception as repr_exc:
                                rendered = f"<repr failed: {type(repr_exc).__name__}>"
                            result_truncated = len(rendered) > MAX_RESULT_REPR_CHARS
                            result_repr = clip(rendered, MAX_RESULT_REPR_CHARS)
                        items.extend(
                            item
                            for item in (dataframe_item(res.result), figure_item(res.result))
                            if item is not None
                        )
                        # Only completed cells contribute figures: a partially
                        # drawn figure from an interrupted cell is noise.
                        capture_new_figures(before_figures, items)
                except BaseException as exc:
                    err = self._error_fields(exc)
                finally:
                    self._restore_sigint(job)
        finally:
            self._restore_sigint(job)
            with contextlib.suppress(OSError):
                os.chdir(previous_cwd)
        execution = {
            "stdout": stdout.getvalue(),
            "stderr": stderr.getvalue(),
            "stdout_truncated": stdout.truncated,
            "stderr_truncated": stderr.truncated,
            "result_repr": result_repr,
            "result_repr_truncated": result_truncated,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "outputs": items,
        }
        if err is not None:
            self._last_error = err
        if job.timed_out:
            err = {
                "type": "TimeoutError",
                "message": f"execution exceeded timeout_s={job.timeout_s}",
                "traceback": err["traceback"] if err else "KeyboardInterrupt (bridge timeout)",
            }
            self._last_error = err
        if err is not None:
            return error(err["type"], err["message"], err["traceback"], execution=execution)
        return ok(execution=execution)

    def _execute_readonly(self, job: _Job) -> dict:
        started = time.monotonic()
        stdout, stderr = BoundedTextIO(), BoundedTextIO()
        previous_cwd = os.getcwd()
        err = None
        payload: dict | None = None
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                try:
                    self._install_sigint(job)
                    if job.interrupt_requested or self._stopping:
                        raise KeyboardInterrupt
                    os.chdir(self.workspace)
                    if job.kind == "vars":
                        payload = ok(**list_variables(self._shell.user_ns))
                    elif job.kind == "inspect":
                        payload = ok(
                            inspect=inspect_dataframe(self._shell.user_ns, job.name, job.n)
                        )
                    elif job.kind == "profile":
                        deadline = time.monotonic() + job.timeout_s
                        payload = ok(
                            profile=profile_dataframe(
                                self._shell.user_ns, job.name, job.config, deadline
                            )
                        )
                    else:  # last_error
                        payload = ok(last_error=self._last_error)
                except BaseException as exc:
                    err = self._error_fields(exc)
                finally:
                    self._restore_sigint(job)
        finally:
            self._restore_sigint(job)
            with contextlib.suppress(OSError):
                os.chdir(previous_cwd)
        execution = {
            "stdout": stdout.getvalue(),
            "stderr": stderr.getvalue(),
            "stdout_truncated": stdout.truncated,
            "stderr_truncated": stderr.truncated,
            "result_repr": None,
            "result_repr_truncated": False,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "outputs": [],
        }
        if job.timed_out:
            err = {
                "type": "TimeoutError",
                "message": f"execution exceeded timeout_s={job.timeout_s}",
                "traceback": err["traceback"] if err else "KeyboardInterrupt (bridge timeout)",
            }
        if err is not None:
            return error(err["type"], err["message"], err["traceback"], execution=execution)
        assert payload is not None
        return payload

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False)
            self._pool = None
