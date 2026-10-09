# Neura V1 — Product & Technical Decisions (Docs)
Status: Draft (living document)  
Last updated: 2026-10-09  
Owner: Neura team

## 1) Summary
Neura V1 is an AI-native assistant for data science work inside **VS Code**, designed to be:
- **data-aware** (profiles real DataFrames)
- **execution-grounded** (runs code, reads outputs/errors, self-corrects)
- **notebook-state aware** (variables/runtime state are first-class)
- **trust-oriented** (guardrails + explicit diffs + local-first runtime)

V1 supports both:
- **Jupyter notebooks (`.ipynb`)** via *attach to an existing local kernel*
- **Python scripts (`.py`) as “notebooks-as-scripts”** via a *managed, persistent Neura runtime* with `# %%` cell markers and a Neura Outputs panel

Remote kernels are explicitly deferred to V2.

---

## 2) V1 Scope

### 2.1 Supported environments
- **Host editor:** VS Code extension (primary and only host in V1)
- **OS:** macOS / Linux / Windows (best-effort; we’ll define official support later)
- **Python:** user-selected interpreter (via VS Code Python extension when available)

### 2.2 Supported data stack
- **pandas-first**
- DataFrame inspection and profiling optimized for pandas DataFrames
- Other libs may work incidentally, but are not V1 targets (e.g., Polars/Spark are V2+)

### 2.3 Primary workflows (V1)
Neura V1 targets these workflows:
1. **Data cleaning**
2. **Debugging**
3. **EDA**
4. **Baseline modeling** (simple sklearn baselines, not AutoML)

### 2.4 Explicit non-goals (V1)
- Remote Jupyter kernels / hosted notebook servers (V2)
- Warehouse connectors beyond what pandas already supports out-of-the-box
- Full experiment tracking platform (we may log locally, but no team system in V1)
- “Statistical correctness proof” (V1 focuses on practical data correctness guardrails)

---

## 3) Core UX Surfaces

### 3.1 VS Code extension surfaces
- **Neura Chat panel**
  - User asks for cleaning/EDA/modeling/debugging help
  - Neura uses tools to inspect data + execute code
  - Neura must present code it ran + what it observed
- **Inline / command-driven actions**
  - Run cell/selection (for `.py`)
  - Fix error (from traceback)
  - Profile DataFrame
  - Generate cleaning plan
  - Create baseline model

### 3.2 Editing model (reviewable)
- Neura proposes changes as **cell-level patches**
  - `.ipynb`: patch a cell or insert a new cell
  - `.py`: patch a `# %%` cell block or insert a new `# %%` cell
- User sees a **diff** and must **Apply** (one-step accept/reject)

### 3.3 Outputs model
- `.ipynb`: outputs render in the notebook as normal
- `.py`: outputs render in a dedicated **Neura Outputs** panel (webview)
  - stdout / stderr
  - DataFrame previews
  - plots (PNG)
  - limited HTML (sanitized)

---

## 4) Execution Architecture (V1)

Neura uses two execution backends with a **common tool/API surface**.

### 4.1 Backend A — `.ipynb` attach mode (local kernels only)
- Neura injects a bootstrap step once per kernel to start an **in-kernel HTTP bridge**:
  - Package: `neura_bridge` (Python)
  - Runs inside the notebook’s existing local Jupyter kernel
- Extension calls the bridge via `127.0.0.1:<port>` + token auth

**V1 constraint:** attach mode supports **local kernels only**.

### 4.2 Backend B — `.py` managed mode (Neura Runtime)
- Neura starts and manages a persistent Python process:
  - Module: `neura_runtime`
  - Exposes the same HTTP API as `neura_bridge`
  - Uses an embedded IPython shell to support notebook-like incremental execution
- Cells are defined via `# %%` markers (and optionally selection-based execution)

### 4.3 Common interface goal
Both backends implement the same minimal capabilities:
- execute code
- list variables
- inspect / profile DataFrames
- return structured outputs (stdout/stderr/tracebacks/plots)

This lets the agent layer treat `.ipynb` and `.py` the same.

---

## 5) Neura Runtime (Managed Mode) — Key Decisions

### 5.1 Lifecycle
- Extension spawns: `python -m neura_runtime --port 0 --token <random> --workspace <path>`
- Runtime prints a single JSON line with port + pid when ready:
  - `{"neura_runtime": true, "port": 51243, "pid": 12345, "version": "0.1.0"}`
- Runtime binds to **127.0.0.1 only**, requires **Bearer token**.

### 5.2 Statefulness
- One persistent runtime namespace per workspace (V1 assumption)
- Execution is serialized (one request at a time)
- Extension provides:
  - Restart runtime
  - Interrupt execution

### 5.3 Output capture (minimum)
- stdout/stderr capture
- traceback capture
- pandas DataFrame preview capture
- matplotlib/seaborn plot capture (Agg → PNG)

---

## 6) Tooling & Agent Behavior

### 6.1 Execution-grounded agent loop (V1 principle)
For agent-driven tasks:
1. plan (short)
2. generate code
3. execute against runtime/kernel
4. parse outputs/errors
5. retry/fix (bounded)
6. run guardrails if data changed
7. propose patch as diff for user approval

**Hard limits (V1):**
- max retries per step (e.g., 3–5)
- execution timeout per step
- output size caps

### 6.2 Minimal tool surface (V1)
Tools exposed to the model (conceptually; implemented via the bridge/runtime API):
- `list_vars()`
- `run_code(code, timeout_s)`
- `inspect_df(var_name, n)`
- `profile_df(var_name, config)`
- `get_last_error()` (optional convenience)

---

## 7) Data Profiling (pandas-first)

### 7.1 Profile contents (budgeted)
- shape, columns
- dtypes
- null counts / %
- approx cardinality (capped)
- numeric summary (min/max/mean/std + quantiles)
- categorical top-k frequencies
- small row samples (truncated; configurable)

### 7.2 Budget controls
- cap rows scanned (first N and/or sampled)
- cap time per profile request (return partial if exceeded)
- cap payload size returned to VS Code

---

## 8) Guardrails (V1 = “data correctness”)
Guardrails run automatically after Neura-driven transformations (and optionally on-demand).

Initial checks:
- **Row count deltas** (unexpected drops/spikes)
- **Null deltas per column**
- **Dtype drift**
- **Duplicate keys** (when keys are provided or confidently inferred)
- **Join explosion heuristics** (many-to-many risk)
- **Large filter loss warning** (dropping a high % of rows)

V1 framing: guardrails are **risk flags**, not guarantees.

---

## 9) LLM Provider Support (V1)

### 9.1 Provider strategy
- Implement a provider-neutral interface internally
- **V1 primary:** OpenAI-compatible API (supports many gateways and “BYOK” patterns)
- **V1 secondary / next:** Anthropic adapter

### 9.2 Requirements
- Tool calling support (or structured function-call equivalent)
- Configurable:
  - provider
  - base URL (for OpenAI-compatible)
  - model name
  - API key
  - token budgets
  - “send samples” toggle + column redaction rules

---

## 10) Runtime/Bridge HTTP API (V1 Draft)

All endpoints require:
- `Authorization: Bearer <token>`

### `GET /health`
Response:
```json
{"ok": true, "status": "ready", "busy": false, "version": "0.1.0"}
```
