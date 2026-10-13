/**
 * The implemented runtime/bridge v0.1 HTTP contract. Both backends speak it;
 * the handshake marker is the only thing that tells them apart.
 */
export type RuntimeBackend = "runtime" | "bridge";
export interface Handshake {
  backend: RuntimeBackend;
  port: number;
  pid: number;
  version: string;
  /** Bridge only: the kernel the bridge is attached to. */
  kernel?: string;
  kernelName?: string;
  connectionFile?: string;
}
export interface RuntimeErrorPayload {
  type: string;
  message: string;
  traceback?: string;
}
/** A bounded rich output item captured during a cell. */
export type RichOutput =
  | { kind: "html"; html: string; truncated: boolean }
  | {
      kind: "image_png";
      format: string;
      data: string;
      bytes: number;
      width: number;
      height: number;
      truncated: boolean;
    }
  | {
      kind: "dataframe";
      shape: [number, number];
      columns: { name: string; dtype: string }[];
      rows: unknown[][];
      row_count: number;
      truncated: boolean;
    }
  | { kind: "text"; text: string; truncated?: boolean };
export interface ExecutionOutput {
  stdout: string;
  stderr: string;
  stdout_truncated: boolean;
  stderr_truncated: boolean;
  result_repr: string | null;
  result_kind: "value" | "none";
  result_repr_truncated: boolean;
  state_changed: boolean;
  rich_truncated: boolean;
  duration_ms: number;
  outputs: RichOutput[];
}
export interface EvalResponse {
  ok: boolean;
  version: string;
  error?: RuntimeErrorPayload;
  execution?: ExecutionOutput;
  busy?: boolean;
}
export interface HealthResponse {
  ok: true;
  version: string;
  busy: boolean;
  status: "ready" | "busy";
}
export interface VariableEntry {
  name: string;
  type: string;
  kind: string;
  shape?: unknown;
  preview: string;
}
export interface VarsResponse {
  ok: boolean;
  version: string;
  vars: VariableEntry[];
  total: number;
  truncated: boolean;
  error?: RuntimeErrorPayload;
}
export interface DataFrameColumn {
  name: string;
  dtype: string;
  null_count: number;
}
export interface DataFramePreview {
  name: string;
  type: string;
  shape: [number, number];
  columns: DataFrameColumn[];
  rows: unknown[][];
  row_count: number;
  more_rows?: boolean;
  truncated: boolean;
}
export interface InspectResponse {
  ok: boolean;
  version: string;
  inspect?: DataFramePreview;
  error?: RuntimeErrorPayload;
}
export interface ProfileColumn {
  name: string;
  dtype: string;
  null_count: number;
  null_pct: number;
  cardinality: number;
  numeric?: {
    min: number | null;
    max: number | null;
    mean: number | null;
    std: number | null;
    quantiles: Record<string, number | null>;
  };
  categorical?: {
    top: { value: string; count: number }[];
    unique_capped: boolean;
    other_count: number;
  };
  datetime?: { min: string | null; max: string | null };
  timedelta?: { min: number | null; max: number | null };
  partial?: boolean;
}
export interface ProfilePayload {
  name: string;
  type: string;
  shape: [number, number];
  rows_scanned: number;
  rows_sampled: boolean;
  partial: boolean;
  duration_ms: number;
  payload_bytes?: number;
  payload_truncated?: boolean;
  columns: ProfileColumn[];
  sample: DataFramePreview | null;
}
export interface ProfileResponse {
  ok: boolean;
  version: string;
  profile?: ProfilePayload;
  error?: RuntimeErrorPayload;
}

export class RuntimeClientError extends Error {
  constructor(
    public readonly kind:
      | "startup"
      | "protocol"
      | "transport"
      | "busy"
      | "cancelled",
    message: string,
  ) {
    super(message);
    this.name = "RuntimeClientError";
  }
}
export function cancelled(
  reason = "Execution cancelled because the runtime was stopped or restarted.",
): RuntimeClientError {
  return new RuntimeClientError("cancelled", reason);
}
function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new RuntimeClientError(
      "protocol",
      "The runtime returned an invalid JSON response.",
    );
  }
  return value as Record<string, unknown>;
}
function version(value: unknown): string {
  if (typeof value !== "string" || !/^0\.1\.\d+$/.test(value)) {
    throw new RuntimeClientError(
      "protocol",
      "Incompatible runtime API. Install Neura runtime 0.1.x in the selected interpreter.",
    );
  }
  return value;
}
export function parseHandshake(line: string): Handshake {
  let value: unknown;
  try {
    value = JSON.parse(line);
  } catch {
    throw new RuntimeClientError(
      "protocol",
      "Runtime startup did not return the expected JSON handshake.",
    );
  }
  const data = object(value);
  const backend: RuntimeBackend | undefined =
    data.neura_runtime === true
      ? "runtime"
      : data.neura_bridge === true
        ? "bridge"
        : undefined;
  if (
    !backend ||
    !Number.isInteger(data.port) ||
    Number(data.port) < 1 ||
    Number(data.port) > 65535 ||
    !Number.isInteger(data.pid) ||
    Number(data.pid) < 1
  ) {
    throw new RuntimeClientError(
      "protocol",
      "Invalid runtime port/PID handshake.",
    );
  }
  const handshake: Handshake = {
    backend,
    port: Number(data.port),
    pid: Number(data.pid),
    version: version(data.version),
  };
  if (typeof data.kernel === "string") {
    handshake.kernel = data.kernel;
  }
  if (typeof data.kernel_name === "string") {
    handshake.kernelName = data.kernel_name;
  }
  if (typeof data.connection_file === "string") {
    handshake.connectionFile = data.connection_file;
  }
  return handshake;
}
export function parseHealth(value: unknown): HealthResponse {
  const data = object(value);
  if (
    data.ok !== true ||
    typeof data.busy !== "boolean" ||
    data.status !== (data.busy ? "busy" : "ready")
  ) {
    throw new RuntimeClientError(
      "protocol",
      "Invalid runtime health response.",
    );
  }
  return {
    ok: true,
    version: version(data.version),
    busy: data.busy,
    status: data.busy ? "busy" : "ready",
  };
}
export function parseEval(value: unknown): EvalResponse {
  const data = object(value);
  if (
    typeof data.ok !== "boolean" ||
    (data.busy !== undefined && typeof data.busy !== "boolean")
  ) {
    throw new RuntimeClientError(
      "protocol",
      "Invalid runtime execution response.",
    );
  }
  const result: EvalResponse = { ok: data.ok, version: version(data.version) };
  if (data.busy !== undefined) {
    result.busy = data.busy as boolean;
  }
  if (data.error !== undefined) {
    const err = object(data.error);
    if (
      typeof err.type !== "string" ||
      typeof err.message !== "string" ||
      (err.traceback !== undefined && typeof err.traceback !== "string")
    ) {
      throw new RuntimeClientError(
        "protocol",
        "Invalid runtime error response.",
      );
    }
    result.error = {
      type: err.type,
      message: err.message,
      traceback: err.traceback as string | undefined,
    };
  }
  if (data.execution !== undefined) {
    result.execution = parseExecution(data.execution);
  }
  if ((result.ok && !result.execution) || (!result.ok && !result.error)) {
    throw new RuntimeClientError(
      "protocol",
      "The runtime response is missing output/error details.",
    );
  }
  return result;
}
function parseRichOutput(value: unknown): RichOutput {
  const item = object(value);
  const kind = item.kind;
  if (kind === "html") {
    if (typeof item.html !== "string") {
      throw new RuntimeClientError("protocol", "Invalid html output item.");
    }
    return { kind: "html", html: item.html, truncated: item.truncated === true };
  }
  if (kind === "image_png") {
    if (typeof item.data !== "string" || typeof item.bytes !== "number") {
      throw new RuntimeClientError("protocol", "Invalid image output item.");
    }
    return {
      kind: "image_png",
      format: typeof item.format === "string" ? item.format : "png",
      data: item.data,
      bytes: item.bytes,
      width: typeof item.width === "number" ? item.width : 0,
      height: typeof item.height === "number" ? item.height : 0,
      truncated: item.truncated === true,
    };
  }
  if (kind === "dataframe") {
    if (!Array.isArray(item.columns) || !Array.isArray(item.rows)) {
      throw new RuntimeClientError("protocol", "Invalid dataframe output item.");
    }
    return {
      kind: "dataframe",
      shape: Array.isArray(item.shape)
        ? [Number(item.shape[0]), Number(item.shape[1])]
        : [0, 0],
      columns: item.columns.map((column) => {
        const entry = object(column);
        return { name: String(entry.name), dtype: String(entry.dtype ?? "") };
      }),
      rows: item.rows as unknown[][],
      row_count: typeof item.row_count === "number" ? item.row_count : 0,
      truncated: item.truncated === true,
    };
  }
  if (typeof item.text === "string") {
    return { kind: "text", text: item.text, truncated: item.truncated === true };
  }
  throw new RuntimeClientError(
    "protocol",
    `Unknown rich output kind: ${String(kind)}`,
  );
}
function parseExecution(value: unknown): ExecutionOutput {
  const out = object(value);
  if (
    typeof out.stdout !== "string" ||
    typeof out.stderr !== "string" ||
    typeof out.stdout_truncated !== "boolean" ||
    typeof out.stderr_truncated !== "boolean" ||
    typeof out.result_repr_truncated !== "boolean" ||
    (out.result_repr !== null && typeof out.result_repr !== "string") ||
    typeof out.duration_ms !== "number" ||
    !Number.isFinite(out.duration_ms) ||
    out.duration_ms < 0 ||
    typeof out.state_changed !== "boolean" ||
    typeof out.rich_truncated !== "boolean" ||
    !Array.isArray(out.outputs)
  ) {
    throw new RuntimeClientError(
      "protocol",
      "Invalid runtime output response.",
    );
  }
  return {
    stdout: out.stdout,
    stderr: out.stderr,
    stdout_truncated: out.stdout_truncated,
    stderr_truncated: out.stderr_truncated,
    result_repr: out.result_repr,
    result_kind: out.result_kind === "none" ? "none" : "value",
    result_repr_truncated: out.result_repr_truncated,
    state_changed: out.state_changed,
    rich_truncated: out.rich_truncated,
    duration_ms: out.duration_ms,
    outputs: out.outputs.map(parseRichOutput),
  };
}
export function parseVars(value: unknown): VarsResponse {
  const data = object(value);
  if (data.ok !== true || !Array.isArray(data.vars)) {
    throw new RuntimeClientError("protocol", "Invalid variables response.");
  }
  return {
    ok: true,
    version: version(data.version),
    vars: data.vars.map((entry) => {
      const item = object(entry);
      return {
        name: String(item.name),
        type: String(item.type),
        kind: String(item.kind),
        shape: item.shape,
        preview: String(item.preview ?? ""),
      };
    }),
    total: typeof data.total === "number" ? data.total : 0,
    truncated: data.truncated === true,
  };
}
export function parseInspect(value: unknown): InspectResponse {
  const data = object(value);
  if (typeof data.ok !== "boolean") {
    throw new RuntimeClientError("protocol", "Invalid inspect response.");
  }
  const response: InspectResponse = {
    ok: data.ok,
    version: version(data.version),
  };
  if (data.error !== undefined) {
    response.error = data.error as RuntimeErrorPayload;
  }
  if (data.inspect !== undefined) {
    const preview = object(data.inspect);
    response.inspect = {
      name: String(preview.name),
      type: String(preview.type),
      shape: Array.isArray(preview.shape)
        ? [Number(preview.shape[0]), Number(preview.shape[1])]
        : [0, 0],
      columns: (Array.isArray(preview.columns) ? preview.columns : []).map(
        (column) => {
          const entry = object(column);
          return {
            name: String(entry.name),
            dtype: String(entry.dtype ?? ""),
            null_count: Number(entry.null_count ?? 0),
          };
        },
      ),
      rows: (preview.rows as unknown[][]) ?? [],
      row_count: Number(preview.row_count ?? 0),
      more_rows: preview.more_rows === true,
      truncated: preview.truncated === true,
    };
  }
  return response;
}
export function parseProfile(value: unknown): ProfileResponse {
  const data = object(value);
  if (typeof data.ok !== "boolean") {
    throw new RuntimeClientError("protocol", "Invalid profile response.");
  }
  const response: ProfileResponse = {
    ok: data.ok,
    version: version(data.version),
  };
  if (data.error !== undefined) {
    response.error = data.error as RuntimeErrorPayload;
  }
  if (data.profile !== undefined) {
    response.profile = data.profile as unknown as ProfilePayload;
  }
  return response;
}
