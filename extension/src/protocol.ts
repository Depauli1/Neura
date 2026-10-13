/** The implemented runtime v0.1 HTTP contract (not a notebook bridge yet). */
export interface Handshake {
  neura_runtime: true;
  port: number;
  pid: number;
  version: string;
}
export interface RuntimeErrorPayload {
  type: string;
  message: string;
  traceback?: string;
}
export interface ExecutionOutput {
  stdout: string;
  stderr: string;
  stdout_truncated: boolean;
  stderr_truncated: boolean;
  result_repr: string | null;
  result_repr_truncated: boolean;
  duration_ms: number;
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
  if (
    data.neura_runtime !== true ||
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
  return {
    neura_runtime: true,
    port: Number(data.port),
    pid: Number(data.pid),
    version: version(data.version),
  };
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
    const out = object(data.execution);
    if (
      typeof out.stdout !== "string" ||
      typeof out.stderr !== "string" ||
      typeof out.stdout_truncated !== "boolean" ||
      typeof out.stderr_truncated !== "boolean" ||
      typeof out.result_repr_truncated !== "boolean" ||
      (out.result_repr !== null && typeof out.result_repr !== "string") ||
      typeof out.duration_ms !== "number" ||
      !Number.isFinite(out.duration_ms) ||
      out.duration_ms < 0
    ) {
      throw new RuntimeClientError(
        "protocol",
        "Invalid runtime output response.",
      );
    }
    result.execution = out as unknown as ExecutionOutput;
  }
  if ((result.ok && !result.execution) || (!result.ok && !result.error)) {
    throw new RuntimeClientError(
      "protocol",
      "The runtime response is missing output/error details.",
    );
  }
  return result;
}
