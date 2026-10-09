# Managed runtime skeleton (Week 1)

The managed `.py` backend now implements startup, health, IPython execution,
interrupts, and budgeted text/error responses. This document describes the
implemented endpoint schemas; the original [API draft](api/runtime-api.md) and
[V1 decisions](v1-decisions.md) are preserved as supplied.

The notebook bridge and VS Code commands remain scaffolds. DataFrame inspection,
profiling, rich display/HTML, and plot capture are not implemented in this step.

## Install and verify

From the repository root, using Python 3.10 or newer:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -e "python[dev]"
pytest python/tests
ruff check python
ruff format --check python
```

On Windows, activate with `.venv\Scripts\Activate.ps1` in PowerShell instead.
The tests spawn real runtime processes and make HTTP calls to their ephemeral
loopback ports; they do not replace the server with an in-process test client.

The extension scaffold can still be checked with:

```sh
cd extension
npm install
npm run compile
```

## Launch contract

The extension will generate a fresh token for each runtime. For manual testing
in a POSIX shell:

```sh
TOKEN=$(python -c 'import secrets; print(secrets.token_urlsafe(32))')
python -m neura_runtime --port 0 --token="$TOKEN" --workspace "$PWD"
```

The `--token=...` form also handles URL-safe random tokens beginning with `-`;
use that form when spawning the process from the extension.

`--token` and `--workspace` are required; the workspace must be an existing
directory. `--port` defaults to `0` (an OS-assigned free port), or accepts an
explicit port between 1 and 65535. There is no `--host` option: the socket is
always bound to **127.0.0.1**, never to external interfaces or IPv6.

Once the HTTP listener is ready, stdout receives one flushed JSON line:

```json
{"neura_runtime": true, "port": 51243, "pid": 12345, "version": "0.1.0"}
```

The actual port and PID vary. The same socket is retained from binding through
server startup, so discovering a free port does not require closing/rebinding it.
Startup failure exits nonzero without a handshake. Server diagnostics use stderr;
neither the handshake nor server logs include the token.

IPython runs on the main thread; uvicorn and HTTP handlers run separately. The
namespace persists across requests, including IPython magics and `_`/`Out`
semantics. The workspace is on Python's import path for local modules. An isolated
temporary IPython profile and an in-memory history database avoid changing the
user's interactive profile/history.

On POSIX, SIGINT or SIGTERM requests graceful shutdown and interrupts an active
cell. The extension can restart by stopping the process and spawning a new one;
there is no HTTP restart endpoint. On Windows, process termination is normally
forced by the process owner.

## Authentication and response format

Every HTTP request, including `/health` and unknown paths, requires:

```text
Authorization: Bearer <token>
```

Missing or incorrect tokens return HTTP 401 with `WWW-Authenticate: Bearer`.
Automatic docs and OpenAPI routes are disabled. Responses use
`application/json; charset=utf-8`, with `ok` and `version` in the envelope.

In a second terminal, set `PORT` to the handshake's port and `TOKEN` to the token
used to launch the process. For example:

```sh
curl -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:$PORT/health"
curl -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"code":"answer = 40\nprint(\"hello\")\nanswer + 2","timeout_s":5}' \
  "http://127.0.0.1:$PORT/eval"
```

### `GET /health`

```json
{"ok": true, "version": "0.1.0", "status": "ready", "busy": false}
```

While an eval is accepted/running, `status` is `"busy"` and `busy` is `true`.
Health and interrupt calls remain serviceable during ordinary Python execution.

### `POST /eval`

JSON request:

```json
{"code": "answer = 40\nprint('hello')\nanswer + 2", "timeout_s": 5, "cwd": null}
```

- `code`: required string. Empty code is a successful no-op.
- `timeout_s`: optional finite number, strictly greater than 0 and at most 3600;
  defaults to **30 seconds**. Null, booleans, NaN, and infinity are invalid.
- `cwd`: optional directory string or null. Relative paths resolve against the
  workspace; omitted/null uses the workspace. Cwd is restored after execution,
  including errors, interrupts, and user calls to `os.chdir`.
- Unknown fields are rejected. Request bodies are limited to **1 MiB**, including
  requests without `Content-Length` (chunked transfer).

Successful response (duration is illustrative):

```json
{
  "ok": true,
  "version": "0.1.0",
  "execution": {
    "stdout": "hello\n",
    "stderr": "",
    "stdout_truncated": false,
    "stderr_truncated": false,
    "result_repr": "42",
    "result_repr_truncated": false,
    "duration_ms": 3
  }
}
```

The trailing expression is separate from stdout; no `Out[n]` prompts are printed.
`result_repr` is null if there is no result (including a trailing semicolon).
A failing object `repr` is replaced with `<repr failed: ExceptionType>`.

Execution failures still use HTTP **200**, with `ok: false`, the same `execution`
fields (including partial stdout/stderr), and a structured error:

```json
{
  "ok": false,
  "version": "0.1.0",
  "error": {
    "type": "ZeroDivisionError",
    "message": "division by zero",
    "traceback": "Traceback (most recent call last):\n...\nZeroDivisionError: division by zero\n"
  },
  "execution": {
    "stdout": "",
    "stderr": "",
    "stdout_truncated": false,
    "stderr_truncated": false,
    "result_repr": null,
    "result_repr_truncated": false,
    "duration_ms": 2
  }
}
```

Syntax errors, runtime exceptions, SystemExit, and KeyboardInterrupt are returned
as structured errors rather than killing the runtime. IPython's ANSI traceback
printing is suppressed. Changes made before an error/timeout are **not rolled back**.

Only one eval is accepted at a time. A second request returns HTTP **409**
immediately with an error of type `BusyError`; it is not queued or executed.
The extension should serialize its eval calls.

### `POST /interrupt`

No body is required. The response reports whether an active job was targeted:

```json
{"ok": true, "version": "0.1.0", "interrupted": true}
```

Idle interrupts return `interrupted: false` and do nothing. The eval's eventual
response contains `KeyboardInterrupt` unless the code catches it or a timeout
has already elapsed (then `TimeoutError` takes precedence).

## Budgets, timeouts, and limitations

- stdout and stderr: first **65,536 characters each**, with truncation flags.
- Result repr: **2,000 characters** maximum, with a truncation flag.
- Error message: **4,000 characters** maximum; traceback: **16,384 characters**.
- Eval timeout: 30 seconds by default; at most 3,600 seconds per request.
- Request body: **1,048,576 bytes** maximum (HTTP 413 if exceeded).
- Invalid JSON/schema: HTTP 422; non-JSON `/eval` content type: HTTP 415;
  shutdown in progress: HTTP 503. These errors use the common error envelope.

Timeouts request a KeyboardInterrupt and return an error of type `TimeoutError`.
On POSIX, a private signal interrupts Python loops and `time.sleep`. On Windows,
`interrupt_main` interrupts Python loops but a blocking native call may not
observe the interrupt until it returns. Native extensions that do not cooperate,
changed signal handlers, and code catching KeyboardInterrupt can defeat interrupts
on any platform. If execution still has not stopped after the timeout plus a
**2-second grace period**, `/eval` returns `TimeoutError` with `busy: true` and no
`execution` snapshot. The execution slot remains reserved until the code actually
stops; `/health` stays busy and new evals keep returning 409. The extension may
need to terminate/restart the process. A timeout is never allowed to spill into
the next cell via an old timer callback.

This skeleton captures Python-level stdout/stderr writes. Direct file-descriptor
writes, subprocess output, and background-thread output routing are not covered;
such writes can still reach the process pipes. Rich display/plot capture is a
later step. The output caps bound returned/captured text, not arbitrary memory
allocations or the size of user objects in the persistent namespace.

**Not a sandbox:** authorized code executes with the user's permissions, can
access files outside the workspace, and can deliberately exit the process or
alter runtime internals. Do not expose this port, share its token, or execute
untrusted code. Cross-platform behavior beyond the automated Linux checks is
best-effort until an official support matrix is established.
