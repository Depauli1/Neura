"""One persistent IPython shell, executed on the process's main thread.

HTTP handlers run on other threads and submit jobs here. Only one job can be
accepted at a time. Keeping user code on the main thread lets a POSIX signal
interrupt both Python loops and blocking calls such as ``time.sleep`` without
blocking the HTTP server. On Windows, ``interrupt_main`` is best-effort and
cannot interrupt a blocking native call until it returns.

Besides ``eval``, the executor serves the read-only inspection surface
(``list_vars``/``inspect_df``/``profile_df``/``last_error``) through the same
slot, so the namespace is never read while user code mutates it.

Timeouts are scoped to a job, not merely to the runtime's busy flag: a late
callback or signal must never interrupt the next cell. This is not a sandbox;
user code can catch interrupts, change signal handlers, or terminate the process.
"""

from __future__ import annotations

import _thread
import contextlib
import os
import queue
import signal
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass, field

from IPython.core.interactiveshell import InteractiveShell
from traitlets.config import Config

from neura_core.api import error, ok
from neura_core.execution import (
    DEFAULT_INSPECT_TIMEOUT_S,
    DEFAULT_TIMEOUT_S,
    INTERRUPT_GRACE_S,
    MAX_MESSAGE_CHARS,
    MAX_OUTPUT_CHARS,
    MAX_RESULT_REPR_CHARS,
    MAX_TRACEBACK_CHARS,
    BusyError,
    RuntimeStoppingError,
    clip,
    execution_payload,
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
from neura_core.variables import list_variables, namespace_fingerprint

__all__ = [
    "MAX_OUTPUT_CHARS",
    "MAX_RESULT_REPR_CHARS",
    "BusyError",
    "RuntimeStoppingError",
    "Executor",
]


class _RuntimeShell(InteractiveShell):
    @contextlib.contextmanager
    def _tee(self, channel):
        # Recent IPython versions duplicate every write into unbounded output
        # history. Our own bounded captures must be the only stream collectors.
        yield

    # IPython normally prints colored tracebacks into stdout. We instead use
    # ExecutionResult.error_before_exec/error_in_exec to return structured errors.
    def showtraceback(self, *args, **kwargs) -> None:
        pass

    def showsyntaxerror(self, *args, **kwargs) -> None:
        pass


@dataclass
class _Job:
    kind: str  # "eval" | "vars" | "inspect" | "profile" | "last_error"
    code: str = ""
    timeout_s: float = DEFAULT_TIMEOUT_S
    cwd: str | None = None
    name: str = ""
    n: int = 5
    config: dict | None = None
    done: threading.Event = field(default_factory=threading.Event)
    result: dict | None = None
    started: bool = False
    timed_out: bool = False
    interrupt_requested: bool = False


class Executor(BaseExecutor):
    """Main-thread executor; HTTP worker threads call :meth:`eval` and friends."""

    def __init__(self, workspace: str) -> None:
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("Executor must be created and run on the main thread")
        self.workspace = os.path.abspath(workspace)
        self._queue: queue.Queue[_Job] = queue.Queue()
        self._state_lock = threading.Lock()
        self._active: _Job | None = None
        self._running: _Job | None = None
        self._pending_interrupt: _Job | None = None
        self._stopping = False
        self._previous_handlers: dict = {}
        self._last_error: dict | None = None
        self._interrupt_signal = getattr(signal, "SIGUSR1", signal.SIGINT)
        # Isolate IPython's profile and keep its history database in memory.
        self._profile = tempfile.TemporaryDirectory(prefix="neura-ipython-")
        cfg = Config()
        cfg.HistoryManager.hist_file = ":memory:"
        cfg.InteractiveShell.cache_size = 100
        try:
            self._shell = _RuntimeShell.instance(
                config=cfg, ipython_dir=self._profile.name, displayhook_class=QuietResultHook
            )
            self._install_signal_handlers()
        except BaseException:
            self._profile.cleanup()
            raise

    # ------------------------------------------------------------------ shell

    @property
    def shell(self):
        """The persistent IPython shell (its ``user_ns`` is the namespace)."""
        return self._shell

    def _install_signal_handlers(self) -> None:
        handlers = {signal.SIGINT: self._on_sigint, signal.SIGTERM: self._on_stop_signal}
        if self._interrupt_signal != signal.SIGINT:
            handlers[self._interrupt_signal] = self._on_interrupt_signal
        for signum, handler in handlers.items():
            self._previous_handlers[signum] = signal.signal(signum, handler)

    def _on_interrupt_signal(self, signum, frame) -> None:
        # Do not acquire locks in signal handlers: the interrupted main thread
        # might already hold one. Identity checks discard stale notifications.
        target = self._pending_interrupt
        self._pending_interrupt = None
        if target is not None and self._running is target:
            raise KeyboardInterrupt

    def _on_sigint(self, signum, frame) -> None:
        if self._interrupt_signal == signal.SIGINT and self._pending_interrupt is not None:
            self._on_interrupt_signal(signum, frame)
        else:
            self._on_stop_signal(signum, frame)

    def _on_stop_signal(self, signum, frame) -> None:
        self._stopping = True
        if self._running is not None:
            raise KeyboardInterrupt

    @property
    def busy(self) -> bool:
        with self._state_lock:
            return self._active is not None

    # ------------------------------------------------------------- submission

    def _submit(self, kind: str, timeout_s: float, **fields) -> dict:
        """Reserve the execution slot immediately; never queue behind a job."""
        job = _Job(kind=kind, timeout_s=timeout_s, **fields)
        with self._state_lock:
            if self._stopping:
                raise RuntimeStoppingError("runtime is shutting down")
            if self._active is not None:
                raise BusyError("runtime is busy executing another request")
            self._active = job
            self._queue.put(job)
        if not job.done.wait(timeout_s + INTERRUPT_GRACE_S):
            self._request_interrupt(job, timed_out=True)
            # Keep the slot reserved until the main thread actually finishes.
            # Never report ready while an uninterruptible cell is still running.
            return error(
                "TimeoutError",
                f"execution exceeded timeout_s={timeout_s}; "
                "interrupt requested but execution is still running",
                busy=True,
            )
        assert job.result is not None
        return job.result

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
        """Request an interrupt, returning False if there is no active job."""
        with self._state_lock:
            job = self._active
        return self._request_interrupt(job) if job is not None else False

    def stop(self) -> None:
        self.shutdown()

    def _request_interrupt(self, job: _Job | None, *, timed_out: bool = False) -> bool:
        if job is None:
            return False
        with self._state_lock:
            if self._active is not job or job.done.is_set():
                return False
            if timed_out and job.timed_out:
                return False  # one timeout notification per job
            if job.started and self._running is not job:
                return False  # completed user code; already in cleanup
            job.interrupt_requested = True
            job.timed_out |= timed_out
            notify = self._running is job
            if notify:
                self._pending_interrupt = job
        if notify:
            if self._interrupt_signal == signal.SIGINT:
                _thread.interrupt_main()
            else:
                os.kill(os.getpid(), self._interrupt_signal)
        return True

    # ------------------------------------------------------------- main loop

    def run_forever(self) -> None:
        """Drain accepted jobs on the MAIN thread until shutdown is requested."""
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("run_forever must run on the main thread")
        while not self._stopping:
            try:
                job = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                job.result = self._execute(job)
            except BaseException as exc:
                # A failure in bookkeeping must not strand a waiting HTTP caller.
                job.result = error(type(exc).__name__, "execution failed in runtime bookkeeping")
            finally:
                self._running = None
                with self._state_lock:
                    self._active = None
                    if self._pending_interrupt is job:
                        self._pending_interrupt = None
                # Release busy BEFORE waking the caller, so its next eval works.
                job.done.set()
        # A shutdown can arrive between accepting a job and taking it off the queue.
        with self._state_lock:
            if self._active is not None:
                self._active.result = error("RuntimeStoppingError", "runtime is shutting down")
                self._active.done.set()
                self._active = None

    def shutdown(self) -> None:
        self._stopping = True
        self.interrupt()

    def close(self) -> None:
        """Restore handlers and release IPython resources (on the main thread)."""
        for signum, handler in self._previous_handlers.items():
            signal.signal(signum, handler)
        self._previous_handlers.clear()
        try:
            self._shell._atexit_once()
        finally:
            _RuntimeShell.clear_instance()
            self._profile.cleanup()

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
        before_figures = figure_numbers()
        before_namespace = namespace_fingerprint(self._shell.user_ns)
        dropped_figures = 0
        timer = threading.Timer(
            job.timeout_s, self._request_interrupt, args=(job,), kwargs={"timed_out": True}
        )
        timer.daemon = True
        err = None
        result_repr = None
        result_truncated = False
        items: list[dict] = []
        before_figures = figure_numbers()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                try:
                    self._running = job
                    job.started = True
                    timer.start()
                    if job.interrupt_requested or self._stopping:
                        raise KeyboardInterrupt
                    cwd = os.path.join(self.workspace, job.cwd) if job.cwd else self.workspace
                    os.chdir(cwd)
                    collector = DisplayCollector(items, stdout)
                    original_pub = self._shell.display_pub
                    self._shell.display_pub = collector
                    try:
                        res = self._shell.run_cell(job.code, store_history=True)
                    finally:
                        self._shell.display_pub = original_pub
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
                        dropped_figures = capture_new_figures(before_figures, items)
                except BaseException as exc:
                    err = self._error_fields(exc)
                finally:
                    # Disarm the signal handler before restoring streams/cwd.
                    self._running = None
                    timer.cancel()
        finally:
            self._running = None
            timer.cancel()
            if timer.ident is not None:
                # A cancelled timer may already be in its callback. Wait for
                # that callback before releasing the slot/restoring handlers.
                timer.join()
            with contextlib.suppress(OSError):
                os.chdir(previous_cwd)
        if err is not None:
            self._last_error = err
        if job.timed_out:
            # Even code that caught KeyboardInterrupt exceeded the deadline.
            err = {
                "type": "TimeoutError",
                "message": f"execution exceeded timeout_s={job.timeout_s}",
                "traceback": err["traceback"] if err else "KeyboardInterrupt (runtime timeout)",
            }
            self._last_error = err
        outputs = items
        if dropped_figures:
            outputs = items + [
                {
                    "kind": "text",
                    "text": f"[{dropped_figures} more figures omitted]",
                    "truncated": True,
                }
            ]
        execution = execution_payload(
            stdout=stdout.getvalue(),
            stderr=stderr.getvalue(),
            stdout_truncated=stdout.truncated,
            stderr_truncated=stderr.truncated,
            result_repr=result_repr,
            result_repr_truncated=result_truncated,
            outputs=outputs,
            duration_ms=int((time.monotonic() - started) * 1000),
            state_changed=namespace_fingerprint(self._shell.user_ns) != before_namespace,
        )
        if err is not None:
            return error(err["type"], err["message"], err["traceback"], execution=execution)
        return ok(execution=execution)

    def _execute_readonly(self, job: _Job) -> dict:
        started = time.monotonic()
        stdout, stderr = BoundedTextIO(), BoundedTextIO()
        previous_cwd = os.getcwd()
        timer = threading.Timer(
            job.timeout_s, self._request_interrupt, args=(job,), kwargs={"timed_out": True}
        )
        timer.daemon = True
        err = None
        payload: dict | None = None
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                try:
                    self._running = job
                    job.started = True
                    timer.start()
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
                    self._running = None
                    timer.cancel()
        finally:
            self._running = None
            timer.cancel()
            if timer.ident is not None:
                timer.join()
            with contextlib.suppress(OSError):
                os.chdir(previous_cwd)
        execution = execution_payload(
            stdout=stdout.getvalue(),
            stderr=stderr.getvalue(),
            stdout_truncated=stdout.truncated,
            stderr_truncated=stderr.truncated,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        if job.timed_out:
            err = {
                "type": "TimeoutError",
                "message": f"execution exceeded timeout_s={job.timeout_s}",
                "traceback": err["traceback"] if err else "KeyboardInterrupt (runtime timeout)",
            }
        if err is not None:
            return error(err["type"], err["message"], err["traceback"], execution=execution)
        assert payload is not None
        return payload
