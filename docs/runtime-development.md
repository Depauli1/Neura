# Runtime and bridge development

Neura has two interchangeable Python backends behind one HTTP surface:

- **`neura_runtime`** — the managed `.py` backend. It owns a fresh IPython
  process, binds a loopback port, and prints a handshake line on startup.
- **`neura_bridge`** — the notebook attach backend. It runs *inside* an
  already-live Jupyter/IPython kernel (VS Code notebooks, JupyterLab, plain
  `ipykernel`), so execution shares the notebook's own namespace.

Both implement the endpoints documented here and in
[the shared API contract](api/runtime-api.md); the extension treats them
interchangeably and only tells them apart by the handshake marker.

The [VS Code execution slice](extension-development.md) wires the managed runtime
to cell/selection execution and an Outputs panel. The bridge, the
`/vars`, `/inspect`, `/profile` and `/last_error` inspection surface, and rich
display/HTML/plot capture are now implemented too.

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

`python[dev]` includes the kernel packages the bridge and its tests need
(`ipykernel`, `jupyter_client`, `jupyter_core`). Install `python[bridge]` alone
when you only want attach mode inside an existing kernel.

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

The extension generates a fresh token for each runtime. For manual testing
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
    "result_kind": "value",
    "result_repr_truncated": false,
    "state_changed": true,
    "rich_truncated": false,
    "duration_ms": 3,
    "outputs": []
  }
}
```

The trailing expression is separate from stdout; no `Out[n]` prompts are printed.
`result_repr` is null if there is no result (including a trailing semicolon),
and `result_kind` is then `"none"`. A failing object `repr` is replaced with
`<repr failed: ExceptionType>`.

`state_changed` reports whether the cell added, removed or rebound a name in the
namespace (in-place mutation of an existing object is not detected; re-run
`/vars` when that matters). `rich_truncated` is true when any entry of `outputs`
was clipped or omitted — see
[Rich output capture](#rich-output-capture-outputs).

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
    "result_kind": "none",
    "result_repr_truncated": false,
    "state_changed": false,
    "rich_truncated": false,
    "duration_ms": 2,
    "outputs": []
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

### `GET /vars`

Summarizes the user-visible namespace. Private/dunder names, modules, callables
and classes are omitted; IPython's own `In`/`Out` history is not reported.

```json
{
  "ok": true,
  "version": "0.1.0",
  "vars": [
    {
      "name": "df",
      "type": "DataFrame",
      "kind": "dataframe",
      "shape": [5, 3],
      "preview": "   id  score label\n0   1    1.5     a"
    }
  ],
  "total": 1,
  "truncated": false
}
```

`kind` is one of `dataframe`, `series`, `array`, `figure`, `value`. `preview` is
the first 200 characters of `repr`. At most **200** entries are returned; further
names set `truncated` and are omitted.

### `POST /inspect`

```json
{"name": "df", "n": 5, "timeout_s": 10}
```

Returns a bounded preview of a pandas DataFrame variable:

```json
{
  "ok": true,
  "version": "0.1.0",
  "inspect": {
    "name": "df",
    "type": "DataFrame",
    "shape": [5, 3],
    "columns": [
      {"name": "id", "dtype": "int64", "null_count": 0},
      {"name": "score", "dtype": "float64", "null_count": 1}
    ],
    "rows": [[1, 1.5, "a"]],
    "row_count": 1,
    "more_rows": true,
    "truncated": false
  }
}
```

`n` is clamped to **100** rows; `truncated` reports clipping inside the returned
window, `more_rows` that the frame has rows beyond it. NaN is JSON `null` and
timestamps are ISO strings, so responses are always strict JSON.

Domain failures — unknown name, or a name that is not a DataFrame — return HTTP
**200** with `ok: false` and `error.type == "DataFrameError"`, the same envelope
shape a cell error uses.

### `POST /profile`

```json
{"name": "df", "config": {"max_rows": 100000, "sample_rows": 5, "top_k": 10, "time_budget_s": 5}, "timeout_s": 10}
```

Returns a budgeted statistical profile. All `config` keys are optional and
clamped: `max_rows` (default 10,000,000) bounds the head scan, `sample_rows`
(default 5, at most 50) rides along example rows, `top_k` (default 10, at most
50) bounds categorical value counts, and `time_budget_s` (default 5) is the
wall-clock budget for the whole profile.

```json
{
  "ok": true,
  "version": "0.1.0",
  "profile": {
    "name": "df",
    "type": "DataFrame",
    "shape": [5, 3],
    "rows_scanned": 5,
    "rows_sampled": false,
    "partial": false,
    "duration_ms": 4,
    "payload_bytes": 512,
    "columns": [
      {
        "name": "id",
        "dtype": "int64",
        "null_count": 0,
        "null_pct": 0.0,
        "cardinality": 5,
        "numeric": {
          "min": 1,
          "max": 5,
          "mean": 3.0,
          "std": 1.41,
          "quantiles": {"0.25": 2.0, "0.5": 3.0, "0.75": 4.0}
        }
      },
      {
        "name": "label",
        "dtype": "object",
        "null_count": 0,
        "null_pct": 0.0,
        "cardinality": 3,
        "categorical": {
          "top": [{"value": "a", "count": 2}],
          "unique_capped": false,
          "other_count": 2
        }
      },
      {
        "name": "when",
        "dtype": "datetime64[ns]",
        "null_count": 0,
        "null_pct": 0.0,
        "cardinality": 5,
        "datetime": {"min": "2026-01-01T00:00:00", "max": "2026-01-05T00:00:00"}
      }
    ],
    "sample": {"columns": [], "rows": [], "row_count": 0, "truncated": false}
  }
}
```

Numeric columns report min/max/mean/std and the 25/50/75% quantiles; object and
low-cardinality columns report a top-k value histogram; datetime and timedelta
columns report min/max. `rows_sampled` is true when `max_rows` truncated the
scan. When the budget expires, remaining columns come back with
`"partial": true` and only the statistics computed so far, the top-level
`partial` flag is set, and the sample is dropped. The payload is capped at
**512 KiB**: when a profile would exceed it, the sample is dropped first, then
the histograms, and finally columns, with `payload_truncated: true` set.

### `GET /last_error`

```json
{"ok": true, "version": "0.1.0", "last_error": null}
```

Returns the most recent structured execution error — same `type`/`message`/
`traceback` object `/eval` reports — or `null` when the last cell succeeded.
Errors are never cleared by a successful cell, so an agent can fetch the failure
after the fact.

### `POST /stop`

```json
{"ok": true, "version": "0.1.0", "stopped": true}
```

For the managed runtime this shuts the process down (exit code 0). For the
bridge it stops the HTTP server and **leaves the kernel running**, so the
notebook that owns the kernel keeps working.

## Rich output capture (`outputs`)

`/eval` returns a bounded list of structured items in `execution.outputs`, in
capture order:

| `kind` | Payload | Produced by |
| --- | --- | --- |
| `html` | `html`, `truncated` | `display(HTML(...))`, DataFrames shown with `display()` |
| `image_png` | `format`, `data` (base64), `bytes`, `width`, `height` | matplotlib figures, PNG display data |
| `dataframe` | `shape`, `columns`, `rows`, `row_count`, `truncated` | a DataFrame cell result |
| `text` | `text`, `truncated` | notes such as an oversized image or dropped figures |

Budgets: HTML is clipped to **64 KiB**, at most **3** figures are rendered per
cell at DPI 100 and **200 KiB** each, and DataFrame previews keep at most **50**
rows. Anything clipped, dropped or oversized sets `truncated` on the item and
`rich_truncated` on the execution, so the UI can say so instead of silently
losing data.

Figures are captured from `plt.show()` (Agg) and closed afterwards, so the next
cell never re-renders them. Plain `text/plain` display data still lands on
stdout. The captured HTML is **raw by design**: the runtime executes with the
user's own permissions, so it is not a security boundary — the VS Code webview
sanitizes HTML against an allowlist before rendering it.

## Notebook attach mode (`neura_bridge`)

Attach mode injects the bridge into a kernel that is already running:

```sh
python -m neura_bridge.attach --workspace "$PWD" --token="$TOKEN"
```

The command discovers live kernels by scanning `kernel-*.json` connection files
in the Jupyter runtime directory, probes each for liveness, and — when several
kernels are running — requires `--kernel <connection file>` to disambiguate. It
then executes a small bootstrap cell in the chosen kernel and prints one JSON
line on stdout:

```json
{
  "neura_bridge": true,
  "port": 51243,
  "pid": 12345,
  "version": "0.1.0",
  "kernel": "ZMQInteractiveShell",
  "kernel_name": "python3",
  "connection_file": "/home/you/.local/share/jupyter/runtime/kernel-1234.json"
}
```

The extension launches the process, reads that line, and talks to the same
authenticated HTTP surface described above. `NEURA_BRIDGE_TOKEN` is honored by
`load_ipython_extension()` for kernels started with the bridge preloaded.

**Attach-mode behavior and limits:**

- Loopback-only, token-authenticated, exactly like the managed runtime. The
  token travels inside the injected bootstrap code, which is accepted for V1's
  local-first trust model.
- Bridge cells run on the kernel's own event loop and block it, exactly like a
  notebook cell does. Top-level `await` is not supported in bridge cells.
- Bridge output is captured for the API consumer rather than published into the
  notebook, so running a cell through the API does not change what the user sees
  in their notebook.
- `/interrupt` installs its own SIGINT handler for the duration of a job, which
  raises `KeyboardInterrupt` inside the running cell; the kernel survives it.
- `/stop` stops the bridge only. The kernel and its namespace stay alive.

## Budgets, timeouts, and limitations

- stdout and stderr: first **65,536 characters each**, with truncation flags. A
  clipped stream keeps its prefix and ends with `... [truncated]`.
- Result repr: **2,000 characters** maximum, with a truncation flag.
- Error message: **4,000 characters** maximum; traceback: **16,384 characters**.
- Eval timeout: 30 seconds by default; at most 3,600 seconds per request.
- Inspection calls (`/vars`, `/inspect`, `/profile`, `/last_error`): 10 seconds
  by default, at most 60 seconds, and they serialize against `/eval` — a request
  that arrives while a cell is running returns **409** immediately.
- Request body: **1,048,576 bytes** maximum (HTTP 413 if exceeded).
- Invalid JSON/schema: HTTP 422; non-JSON `/eval` content type: HTTP 415;
  shutdown in progress: HTTP 503. These errors use the common error envelope.

Domain failures (unknown variable, not a DataFrame) are *not* transport errors:
they return HTTP 200 with `ok: false` so a caller can distinguish "the runtime
could not answer" from "the runtime answered: no".

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

Both backends capture Python-level stdout/stderr writes. Direct file-descriptor
writes, subprocess output, and background-thread output routing are not covered;
such writes can still reach the process pipes. The output caps bound
returned/captured text, not arbitrary memory allocations or the size of user
objects in the persistent namespace.

**Not a sandbox:** authorized code executes with the user's permissions, can
access files outside the workspace, and can deliberately exit the process or
alter runtime internals. Do not expose this port, share its token, or execute
untrusted code. Cross-platform behavior beyond the automated Linux checks is
best-effort until an official support matrix is established.
