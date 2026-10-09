"""One persistent IPython shell, executed on the process's main thread.

HTTP handlers run on other threads and submit jobs here. Only one job can be
accepted at a time. Keeping user code on the main thread lets a POSIX signal
interrupt both Python loops and blocking calls such as ``time.sleep`` without
blocking the HTTP server. On Windows, ``interrupt_main`` is best-effort and
cannot interrupt a blocking native call until it returns.

Timeouts are scoped to a job, not merely to the runtime's busy flag: a late
callback or signal must never interrupt the next cell. This is not a sandbox;
user code can catch interrupts, change signal handlers, or terminate the process.
"""

from __future__ import annotations

import _thread
import contextlib
import io
import os
import queue
import signal
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass, field

from IPython.core.displayhook import DisplayHook
from IPython.core.interactiveshell import InteractiveShell
from traitlets.config import Config

from neura_core.api import error, ok

DEFAULT_TIMEOUT_S = 30.0
MAX_TIMEOUT_S = 3600.0
MAX_OUTPUT_CHARS = 64 * 1024  # per stream
MAX_RESULT_REPR_CHARS = 2000
MAX_TRACEBACK_CHARS = 16 * 1024
INTERRUPT_GRACE_S = 2.0


class BusyError(RuntimeError):
    """Another eval is already active (HTTP 409)."""


class RuntimeStoppingError(RuntimeError):
    """The runtime is shutting down (HTTP 503)."""


class _BoundedTextIO(io.TextIOBase):
    """Keep only the first N characters, but report every write as consumed."""

    def __init__(self, limit: int = MAX_OUTPUT_CHARS) -> None:
        self._buffer = io.StringIO()
        self._limit = limit
        self._size = 0
        self.truncated = False

    @property
    def encoding(self) -> str:
        return "utf-8"

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        remaining = self._limit - self._size
        kept = text[:remaining]
        self._buffer.write(kept)
        self._size += len(kept)
        self.truncated |= len(text) > remaining
        return len(text)

    def getvalue(self) -> str:
        return self._buffer.getvalue()


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = "... [truncated]"
    return text[: limit - len(marker)] + marker


class _ResultHook(DisplayHook):
    """Record the last expression without printing an ``Out[n]`` prompt.

    No rich formatting in this skeleton: the caller returns a bounded repr.
    IPython's underscore/output-history semantics are retained.
    """

    def __call__(self, result=None) -> None:
        self.check_for_underscore()
        if result is not None and not self.quiet():
            self.update_user_ns(result)
            self.fill_exec_result(result)


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
class _EvalJob:
    code: str
    timeout_s: float
    cwd: str | None
    done: threading.Event = field(default_factory=threading.Event)
    result: dict | None = None
    started: bool = False
    timed_out: bool = False
    interrupt_requested: bool = False


class Executor:
    """Main-thread executor; :meth:`eval` is called by HTTP worker threads."""

    def __init__(self, workspace: str) -> None:
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("Executor must be created and run on the main thread")
        self.workspace = os.path.abspath(workspace)
        self._queue: queue.Queue[_EvalJob] = queue.Queue()
        self._state_lock = threading.Lock()
        self._active: _EvalJob | None = None
        self._running: _EvalJob | None = None
        self._pending_interrupt: _EvalJob | None = None
        self._stopping = False
        self._previous_handlers: dict = {}
        self._interrupt_signal = getattr(signal, "SIGUSR1", signal.SIGINT)
        # Isolate IPython's profile and keep its history database in memory.
        self._profile = tempfile.TemporaryDirectory(prefix="neura-ipython-")
        cfg = Config()
        cfg.HistoryManager.hist_file = ":memory:"
        cfg.InteractiveShell.cache_size = 100
        try:
            self._shell = _RuntimeShell.instance(
                config=cfg, ipython_dir=self._profile.name, displayhook_class=_ResultHook
            )
            self._install_signal_handlers()
        except BaseException:
            self._profile.cleanup()
            raise

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

    def eval(self, code: str, timeout_s: float = DEFAULT_TIMEOUT_S, cwd: str | None = None) -> dict:
        """Reserve the execution slot immediately; never queue behind an eval."""
        job = _EvalJob(code, timeout_s, cwd)
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

    def interrupt(self) -> bool:
        """Request an interrupt, returning False if there is no active job."""
        with self._state_lock:
            job = self._active
        return self._request_interrupt(job) if job is not None else False

    def _request_interrupt(self, job: _EvalJob, *, timed_out: bool = False) -> bool:
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

    @staticmethod
    def _error_fields(exc: BaseException) -> dict:
        return {
            "type": type(exc).__name__,
            "message": _clip(str(exc) or type(exc).__name__, 4000),
            "traceback": _clip(
                "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
                MAX_TRACEBACK_CHARS,
            ),
        }

    def _execute(self, job: _EvalJob) -> dict:
        started = time.monotonic()
        stdout, stderr = _BoundedTextIO(), _BoundedTextIO()
        previous_cwd = os.getcwd()
        timer = threading.Timer(
            job.timeout_s, self._request_interrupt, args=(job,), kwargs={"timed_out": True}
        )
        timer.daemon = True
        err = None
        result_repr = None
        result_truncated = False
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
                    res = self._shell.run_cell(job.code, store_history=True)
                    exc = res.error_before_exec or res.error_in_exec
                    if exc is not None:
                        err = self._error_fields(exc)
                    elif res.result is not None:
                        try:
                            rendered = repr(res.result)
                        except Exception as exc:
                            rendered = f"<repr failed: {type(exc).__name__}>"
                        result_truncated = len(rendered) > MAX_RESULT_REPR_CHARS
                        result_repr = _clip(rendered, MAX_RESULT_REPR_CHARS)
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
        execution = {
            "stdout": stdout.getvalue(),
            "stderr": stderr.getvalue(),
            "stdout_truncated": stdout.truncated,
            "stderr_truncated": stderr.truncated,
            "result_repr": result_repr,
            "result_repr_truncated": result_truncated,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        if job.timed_out:
            # Even code that caught KeyboardInterrupt exceeded the deadline.
            err = {
                "type": "TimeoutError",
                "message": f"execution exceeded timeout_s={job.timeout_s}",
                "traceback": err["traceback"] if err else "KeyboardInterrupt (runtime timeout)",
            }
        if err is not None:
            return error(err["type"], err["message"], err["traceback"], execution=execution)
        return ok(execution=execution)
