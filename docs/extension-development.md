# VS Code `.py` execution — first usable vertical slice

Neura now connects the VS Code commands to the managed Python runtime. This is
not complete V1: notebooks, DataFrame/plot previews, profiling, AI chat, patches,
and guardrails remain later milestones.

## Try it in VS Code

1. Open the **repository root** in desktop VS Code and trust the workspace.
2. Create/select a Python 3.10+ environment and install the runtime in it:

   ```sh
   python -m venv .venv
   . .venv/bin/activate
   python -m pip install -e "python[dev]"
   ```

   Windows PowerShell activation: `.venv\Scripts\Activate.ps1`.

3. Install extension development dependencies:

   ```sh
   cd extension
   npm ci
   npm run compile
   ```

4. Press **F5**, selecting **Neura Extension**. The checked-in launch/task files
   compile the extension and open an Extension Development Host with this repo.
5. In that new window, select the environment from step 2 using the Microsoft
   Python extension, or set **Neura: Python Path** to its executable. For example,
   in workspace settings for this repository:

   ```json
   { "neura.pythonPath": ".venv/bin/python" }
   ```

   On Windows use `.venv\\Scripts\\python.exe` in the JSON string instead.
6. Open `fixtures/runtime-demo.py`. Run its cells using the Command Palette or
   the shortcuts below. **Neura Outputs** opens beside the editor without moving
   focus away from it.

If the interpreter cannot import `neura_runtime`, startup reports the error and
an installation hint. **Neura: Show Runtime Log** exposes diagnostics. Install
in the *same* interpreter the extension uses; it does not install packages for
you or silently switch an explicitly selected environment.

## Commands and behavior

| Command | Behavior |
| --- | --- |
| **Neura: Run Cell** | Run the `# %%` cell at the cursor. Cursor on a marker runs the following body. |
| **Neura: Run Selection** | Run exactly the selected text. With no selection, run the current line (leading indentation removed). |
| **Neura: Show Outputs Panel** | Reveal text outputs, execution history, and runtime controls. |
| **Neura: Restart Runtime** | Cancel active/older queued runs, stop the process, and start a new empty namespace using the currently selected interpreter. |
| **Neura: Interrupt** | Request best-effort interruption immediately, outside the execution queue. |
| **Neura: Show Runtime Log** | Reveal startup/lifecycle diagnostics, with Bearer tokens redacted. |

Run Cell: **Ctrl+Alt+Enter** (macOS **Cmd+Alt+Enter**).
Run Selection: **Ctrl+Alt+R** (macOS **Cmd+Alt+R**).
Both are also available in the Python editor's context menu. Bindings are
contributed only while a local `.py` editor is focused; VS Code's Keyboard
Shortcuts editor can customize them.

- Executes the **editor buffer**, including unsaved changes; does not save or
  modify the file. Selection indentation is otherwise preserved, so incomplete
  or indented fragments can legitimately return Python syntax errors.
- A file without markers is one cell. Text before the first marker is an
  implicit preamble cell. CRLF works; marker-looking text inside ordinary or
  triple-quoted strings is ignored. `# %% [markdown]` cells are not executed.
- Uses one persistent namespace **per workspace folder**, shared by that folder's
  `.py` files. Open a folder; loose files, untitled documents, notebook cells,
  virtual workspaces, and remote URI schemes are not supported in this milestone.
- Cwd is the workspace folder, not the script's containing directory. Local
  imports and relative files follow the runtime's documented behavior.
- Starts lazily on the first execution. Subsequent runs reuse the process. Calls
  are serialized in command invocation order, with at most 20 active/queued runs.
- Interrupt targets the active cell; it does **not** cancel queued cells.
  **Restart** cancels both the active request and older queued cells. A crash
  also cancels older queued work rather than replaying it into a fresh namespace.
- Workspace removal and extension deactivation stop owned processes. On POSIX,
  a separate process group is terminated, then force-killed if needed; on Windows
  `taskkill /T /F` handles the process tree. Restart refuses to launch another
  process if the old process cannot be stopped.

## Outputs panel

The panel shows workspace/runtime state, interpreter, PID, pending count, and
per-execution cards with source file/lines, unsaved-buffer indication, expandable
executed code, stdout, stderr, trailing-expression repr, duration, and structured
errors/tracebacks. Source buttons jump back to the editor. The toolbar switches
workspace, interrupts/restarts, opens diagnostics, or clears that workspace's
visible history.

History is in memory only, bounded to the latest 50 entries across workspaces.
Code display is capped at 16,000 characters per entry; execution still receives
the original code. Text output limits come from the runtime. HTML/script-looking
Python output is escaped, terminal color controls are removed, and a nonce-based
Content Security Policy protects the webview. No rich HTML, DataFrames, or PNG
plots are rendered yet.

Output is returned at completion, **not streamed**. If a timeout leaves native
code running, the runtime stays busy and the panel says so. New queued calls are
rejected rather than sent into the occupied namespace. Health polling detects
when it becomes ready; interrupt/restart remain available. Interrupt is
best-effort and may not stop code that catches interrupts or blocks in native
extensions. See the [runtime guide](runtime-development.md).

## Interpreter and settings

Resolution order:

1. Resource-scoped **`neura.pythonPath`**, if nonempty. A literal executable path
   or PATH command; no shell arguments/quoting. Relative paths resolve against
   the workspace folder. Paths containing spaces are supported.
2. Microsoft Python extension's selected environment API; legacy execution
   command arrays are supported for wrappers such as `conda run`.
3. `python3` on PATH (Windows: `python`), **only if the Python extension is absent**.

Python extension selection failures do not silently fall back to another
interpreter. Restart after changing an interpreter or the override; existing
sessions intentionally retain their interpreter/namespace until then.

| Setting | Default | Purpose |
| --- | --- | --- |
| `neura.pythonPath` | empty | Override the interpreter executable; install `neura_runtime` there. |
| `neura.executionTimeoutSeconds` | 30 | Per-run best-effort deadline; > 0, <= 3600. |
| `neura.startupTimeoutSeconds` | 30 | Startup handshake wait; > 0, <= 120. |

These are folder/resource-scoped in multi-root workspaces. Workspace Trust is
required: execution has user permissions, not sandbox permissions. The Node
extension host alone calls the authenticated loopback API; the webview never
makes HTTP requests or receives the token. No telemetry, model provider, or
external execution service is used in this milestone.

## Automated checks

After installing the Python runtime in the test interpreter:

```sh
pytest python/tests
cd extension
npm ci
npm test
```

Extension tests default to `python3` (`python` on Windows). Set
`NEURA_TEST_PYTHON` to an executable path if needed:

```sh
NEURA_TEST_PYTHON="$PWD/../.venv/bin/python" npm test
```

Tests cover cell parsing, schema/security rendering, actual command/toolbar
handlers using a small VS Code host fake, interpreter resolution, real
subprocess/socket lifecycle failures, cancellation, response budgets, and a
real `neura_runtime` session (state, streams/errors, serialization, interrupt,
timeout, restart). CI installs Python for the extension integration test, rather
than silently skipping it.

These are **not full Electron/desktop UI tests**. The sandbox has no desktop
VS Code host; Linux process/client tests and host-fake command tests do not
establish Windows/macOS support. Manual smoke-test in the Development Host:

- Run Hello and Persistent state; repeat the latter and observe the counter.
- Select `counter` and run selection; verify the latest value.
- Run Streams and Error capture; observe stderr and a structured traceback.
- Run Interrupt, then press Interrupt; verify later runs still work.
- Restart; run Persistent state again and observe counter 1.
- Queue two slow runs then restart; verify the old queued run is cancelled.
- Close/reopen Outputs; inspect history, use a source button, and clear history.
