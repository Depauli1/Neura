/** Managed-process + authenticated loopback client. No VS Code dependency. */
import { ChildProcess, spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import * as http from "node:http";
import {
  cancelled,
  EvalResponse,
  Handshake,
  parseEval,
  parseHandshake,
  parseHealth,
  RuntimeClientError,
} from "./protocol";

export interface PythonCommand {
  command: string;
  args?: readonly string[];
}
export type RuntimeState =
  | "stopped"
  | "starting"
  | "ready"
  | "busy"
  | "unresponsive"
  | "stopping"
  | "failed";
export interface RuntimeSnapshot {
  state: RuntimeState;
  pid?: number;
  python: string;
  pending: number;
}
export interface RuntimeOptions {
  workspace: string;
  python: PythonCommand;
  startupTimeoutMs?: number;
  onState?: (snapshot: RuntimeSnapshot) => void;
  onLog?: (message: string) => void;
}
const MAX_REQUEST_BYTES = 1024 * 1024;
const MAX_RESPONSE_BYTES = 1024 * 1024;
const MAX_PENDING = 20;

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
function alive(child: ChildProcess): boolean {
  return !!child.pid && child.exitCode === null && child.signalCode === null;
}
function waitForExit(child: ChildProcess, ms: number): Promise<boolean> {
  if (!alive(child)) {
    return Promise.resolve(true);
  }
  return new Promise((resolve) => {
    const done = () => {
      clearTimeout(timer);
      child.removeListener("exit", done);
      resolve(true);
    };
    const timer = setTimeout(() => {
      child.removeListener("exit", done);
      resolve(!alive(child));
    }, ms);
    child.once("exit", done);
  });
}
async function signalTree(child: ChildProcess, force: boolean): Promise<void> {
  if (!child.pid) {
    return;
  }
  if (process.platform === "win32") {
    // Windows has no POSIX process-group signals. Include wrapper/child processes.
    await new Promise<void>((resolve) => {
      const killer = spawn(
        "taskkill.exe",
        ["/PID", String(child.pid), "/T", "/F"],
        {
          windowsHide: true,
          stdio: "ignore",
          shell: false,
        },
      );
      killer.once("error", () => {
        child.kill();
        resolve();
      });
      killer.once("exit", () => resolve());
    });
  } else {
    try {
      process.kill(-child.pid, force ? "SIGKILL" : "SIGTERM");
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ESRCH") {
        throw error;
      }
    }
  }
}

export class ManagedRuntime {
  private python: PythonCommand;
  private state: RuntimeState = "stopped";
  private child?: ChildProcess;
  private handshake?: Handshake;
  private token = "";
  private epoch = 0;
  private cancellationReason: string | undefined;
  private disposed = false;
  private pending = 0;
  private tail: Promise<void> = Promise.resolve();
  private startup?: { epoch: number; promise: Promise<void> };
  private stopPromise?: Promise<void>;
  private restartPromise?: Promise<void>;
  private cancelStartup?: () => void;
  private requests = new Set<http.ClientRequest>();
  private poll?: NodeJS.Timeout;
  private inFlight = false;

  constructor(private readonly options: RuntimeOptions) {
    this.python = options.python;
  }
  get snapshot(): RuntimeSnapshot {
    return {
      state: this.state,
      pid: this.handshake?.pid,
      python: this.python.command,
      pending: this.pending,
    };
  }
  private notify(): void {
    this.options.onState?.(this.snapshot);
  }
  private setState(state: RuntimeState): void {
    this.state = state;
    this.notify();
  }
  private log(text: string, token = this.token): void {
    this.options.onLog?.(token ? text.split(token).join("[redacted]") : text);
  }
  private check(epoch: number): void {
    if (this.disposed || epoch !== this.epoch) {
      throw cancelled(this.cancellationReason);
    }
  }
  private clearPoll(): void {
    clearTimeout(this.poll);
    this.poll = undefined;
  }

  /** Concurrent first runs share a single startup. */
  async start(): Promise<void> {
    if (this.disposed) {
      throw cancelled();
    }
    if (this.restartPromise) {
      return this.restartPromise;
    }
    if (this.stopPromise) {
      await this.stopPromise;
    }
    if (this.disposed) {
      throw cancelled();
    }
    if (this.child && alive(this.child) && this.handshake) {
      return;
    }
    if (this.startup?.epoch === this.epoch) {
      return this.startup.promise;
    }
    if (this.child && alive(this.child)) {
      throw new RuntimeClientError(
        "transport",
        "The previous runtime is still alive but disconnected. Restart it; refusing to launch a second process.",
      );
    }
    return this.launch(this.epoch);
  }

  private launch(epoch: number): Promise<void> {
    this.check(epoch);
    this.setState("starting");
    this.token = randomBytes(32).toString("hex");
    const token = this.token; // Keep this process's secret even across a later restart.
    const child = spawn(
      this.python.command,
      [
        ...(this.python.args ?? []),
        "-m",
        "neura_runtime",
        "--port",
        "0",
        `--token=${this.token}`,
        "--workspace",
        this.options.workspace,
      ],
      {
        cwd: this.options.workspace,
        env: { ...process.env, PYTHONUNBUFFERED: "1" },
        shell: false,
        windowsHide: true,
        detached: process.platform !== "win32",
        stdio: ["ignore", "pipe", "pipe"],
      },
    );
    this.child = child;
    let stderr = "";
    let stderrLine = "";
    let stdout = "";
    let stdoutLine = "";
    let accepted = false;
    const ready = new Promise<Handshake>((resolve, reject) => {
      let settled = false;
      const finish = (error?: Error, handshake?: Handshake) => {
        if (settled) {
          return;
        }
        settled = true;
        clearTimeout(timer);
        if (this.cancelStartup === cancel) {
          this.cancelStartup = undefined;
        }
        if (error) {
          reject(error);
        } else {
          resolve(handshake!);
        }
      };
      const cancel = () => finish(cancelled());
      this.cancelStartup = cancel;
      const timer = setTimeout(
        () =>
          finish(
            new RuntimeClientError(
              "startup",
              "Runtime startup timed out. Check the selected interpreter and Neura Runtime log.",
            ),
          ),
        this.options.startupTimeoutMs ?? 30000,
      );
      child.once("error", (error) =>
        finish(
          new RuntimeClientError(
            "startup",
            `Could not launch Python (${this.python.command}): ${error.message}`,
          ),
        ),
      );
      child.once("exit", (code, sig) => {
        if (stderrLine) {
          this.log(stderrLine, token);
          stderrLine = "";
        }
        if (stdoutLine) {
          this.log(`stdout: ${stdoutLine}`, token);
          stdoutLine = "";
        }
        finish(
          new RuntimeClientError(
            "startup",
            `Runtime exited before readiness (${sig ?? code}). ${stderr
              .split(token)
              .join("[redacted]")
              .trim()
              .slice(
                -4000,
              )}\nInstall neura_runtime in this interpreter: python -m pip install -e /path/to/Neura/python`,
          ),
        );
        if (this.child === child) {
          this.handshake = undefined;
          this.clearPoll();
          if (
            this.state !== "stopping" &&
            epoch === this.epoch &&
            !this.disposed
          ) {
            if (accepted && this.state !== "starting") {
              this.epoch++; // Never replay older queued cells into a crash-recovery namespace.
              this.inFlight = false;
              this.cancellationReason =
                "Runtime exited unexpectedly. Older queued executions were cancelled; run again to start a fresh session.";
            }
            this.setState("failed");
            if (accepted) {
              this.log(
                `Runtime exited (${sig ?? code}); run again to start a fresh session.`,
              );
            }
          }
          // Clean up descendants even if the interpreter itself has already exited.
          void signalTree(child, true).catch((error) =>
            this.log(message(error)),
          );
        }
      });
      child.stdout!.setEncoding("utf8");
      child.stderr!.setEncoding("utf8");
      child.stderr!.on("data", (chunk: string) => {
        stderr = (stderr + chunk).slice(-32768);
        stderrLine += chunk;
        let newline: number;
        while ((newline = stderrLine.indexOf("\n")) >= 0) {
          this.log(stderrLine.slice(0, newline), token);
          stderrLine = stderrLine.slice(newline + 1);
        }
        // Keep one bounded partial line; don't emit fragments of Bearer tokens.
        if (stderrLine.length > 32768) {
          stderrLine = stderrLine.slice(-32768);
        }
      });
      child.stdout!.on("data", (chunk: string) => {
        if (accepted) {
          stdoutLine += chunk;
          let newline: number;
          while ((newline = stdoutLine.indexOf("\n")) >= 0) {
            this.log(`stdout: ${stdoutLine.slice(0, newline)}`, token);
            stdoutLine = stdoutLine.slice(newline + 1);
          }
          if (stdoutLine.length > 32768) {
            stdoutLine = stdoutLine.slice(-32768);
          }
          return;
        }
        if (settled) {
          return;
        } // Failed handshake: discard further bytes during shutdown.
        stdout += chunk;
        const newline = stdout.indexOf("\n");
        if (newline < 0 && stdout.length <= 8192) {
          return;
        }
        if (newline < 0 || newline > 8192) {
          finish(
            new RuntimeClientError(
              "protocol",
              "Runtime handshake exceeds the startup budget.",
            ),
          );
          return;
        }
        try {
          const handshake = parseHandshake(
            stdout.slice(0, newline).replace(/\r$/, ""),
          );
          accepted = true;
          stdout = "";
          finish(undefined, handshake);
        } catch (error) {
          finish(error as Error);
        }
      });
    });
    const promise = (async () => {
      try {
        const handshake = await ready;
        this.check(epoch);
        if (!alive(child)) {
          throw new RuntimeClientError(
            "startup",
            "Runtime exited during startup.",
          );
        }
        this.handshake = handshake;
        const health = parseHealth(
          await this.request("GET", "/health", undefined, 5000),
        );
        this.check(epoch);
        if (health.version !== handshake.version || health.busy) {
          throw new RuntimeClientError(
            "protocol",
            "Runtime was not ready after its startup handshake.",
          );
        }
        this.log(
          `Ready: PID ${handshake.pid}, API ${handshake.version}, interpreter ${this.python.command}`,
        );
        this.setState("ready");
      } catch (error) {
        if (this.child === child) {
          await this.stopProcess();
        }
        if (epoch === this.epoch && !this.disposed) {
          this.setState("failed");
        }
        throw error;
      }
    })();
    this.startup = { epoch, promise };
    const done = () => {
      if (this.startup?.promise === promise) {
        this.startup = undefined;
      }
    };
    void promise.then(done, done);
    return promise;
  }

  /** Requests are serialized; interrupt and restart deliberately bypass this queue. */
  run(
    code: string,
    timeoutS: number,
    onStarted?: () => void,
  ): Promise<EvalResponse> {
    if (!Number.isFinite(timeoutS) || timeoutS <= 0 || timeoutS > 3600) {
      return Promise.reject(
        new RuntimeClientError(
          "protocol",
          "Execution timeout must be > 0 and <= 3600 seconds.",
        ),
      );
    }
    const body = JSON.stringify({
      code,
      timeout_s: timeoutS,
      cwd: this.options.workspace,
    });
    if (Buffer.byteLength(body, "utf8") > MAX_REQUEST_BYTES) {
      return Promise.reject(
        new RuntimeClientError(
          "protocol",
          "Selected code exceeds the runtime's 1 MiB request limit.",
        ),
      );
    }
    if (this.pending >= MAX_PENDING || this.disposed) {
      return Promise.reject(
        this.disposed
          ? cancelled()
          : new RuntimeClientError(
              "busy",
              "Too many queued executions (maximum 20).",
            ),
      );
    }
    const epoch = this.epoch;
    this.pending++;
    this.notify();
    const task = this.tail.then(async () => {
      this.check(epoch);
      await this.start();
      this.check(epoch);
      try {
        const health = parseHealth(
          await this.request("GET", "/health", undefined, 3000),
        );
        this.check(epoch);
        if (health.busy) {
          this.setState("busy");
          this.schedulePoll(epoch);
          throw new RuntimeClientError(
            "busy",
            "Runtime is still executing. Interrupt it or restart before running more code.",
          );
        }
        this.clearPoll();
        this.inFlight = true;
        this.setState("busy");
        onStarted?.();
        const result = parseEval(
          await this.request("POST", "/eval", body, timeoutS * 1000 + 10000),
        );
        this.check(epoch);
        this.setState(result.busy ? "busy" : "ready");
        if (result.busy) {
          this.schedulePoll(epoch);
        }
        return result;
      } catch (error) {
        this.check(epoch);
        if (this.child && alive(this.child)) {
          try {
            const health = parseHealth(
              await this.request("GET", "/health", undefined, 1500),
            );
            this.check(epoch);
            this.setState(health.busy ? "busy" : "ready");
            if (health.busy) {
              this.schedulePoll(epoch);
            }
          } catch {
            this.check(epoch);
            this.setState("unresponsive");
            this.schedulePoll(epoch);
          }
        }
        throw error;
      } finally {
        if (epoch === this.epoch) {
          this.inFlight = false;
        }
      }
    });
    this.tail = task.then(
      () => undefined,
      () => undefined,
    );
    const done = () => {
      this.pending--;
      this.notify();
    };
    void task.then(done, done);
    return task;
  }

  private schedulePoll(epoch: number): void {
    this.clearPoll();
    this.poll = setTimeout(async () => {
      if (
        this.disposed ||
        epoch !== this.epoch ||
        this.inFlight ||
        !this.handshake
      ) {
        return;
      }
      try {
        const health = parseHealth(
          await this.request("GET", "/health", undefined, 1500),
        );
        this.check(epoch);
        this.setState(health.busy ? "busy" : "ready");
        if (health.busy) {
          this.schedulePoll(epoch);
        }
      } catch {
        if (
          !this.disposed &&
          epoch === this.epoch &&
          this.child &&
          alive(this.child)
        ) {
          this.setState("unresponsive");
          this.schedulePoll(epoch);
        }
      }
    }, 750);
    this.poll.unref();
  }

  async interrupt(): Promise<boolean> {
    if (!this.handshake || !this.child || !alive(this.child) || this.disposed) {
      return false;
    }
    const value = (await this.request(
      "POST",
      "/interrupt",
      "{}",
      3000,
    )) as Record<string, unknown>;
    if (value.ok !== true || typeof value.interrupted !== "boolean") {
      throw new RuntimeClientError("protocol", "Invalid interrupt response.");
    }
    return value.interrupted;
  }

  restart(
    python?: PythonCommand | (() => Promise<PythonCommand>),
  ): Promise<void> {
    if (this.disposed) {
      return Promise.reject(cancelled());
    }
    if (this.restartPromise) {
      return this.restartPromise;
    }
    this.cancellationReason = undefined;
    const epoch = ++this.epoch; // Invalidate ALL previously queued executions immediately.
    const operation = (async () => {
      await this.stopProcess();
      this.check(epoch);
      if (python) {
        this.python = typeof python === "function" ? await python() : python;
      }
      this.check(epoch);
      await this.launch(epoch);
    })();
    this.restartPromise = operation;
    const done = () => {
      if (this.restartPromise === operation) {
        this.restartPromise = undefined;
      }
    };
    void operation.then(done, done);
    return operation;
  }

  async stop(): Promise<void> {
    this.cancellationReason = undefined;
    this.epoch++;
    await this.stopProcess();
  }
  async dispose(): Promise<void> {
    this.disposed = true;
    await this.stop();
  }

  private stopProcess(): Promise<void> {
    if (this.stopPromise) {
      return this.stopPromise;
    }
    this.clearPoll();
    this.setState("stopping");
    this.inFlight = false;
    this.cancelStartup?.();
    for (const request of this.requests) {
      request.destroy(cancelled());
    }
    const child = this.child;
    this.handshake = undefined;
    const operation = (async () => {
      if (child) {
        await signalTree(child, false);
        if (!(await waitForExit(child, 1800))) {
          await signalTree(child, true);
          if (!(await waitForExit(child, 1800))) {
            throw new RuntimeClientError(
              "transport",
              "Could not stop the old runtime; refusing to start a second process.",
            );
          }
        }
        child.stdout?.destroy();
        child.stderr?.destroy();
        if (this.child === child) {
          this.child = undefined;
        }
      }
      this.setState("stopped");
    })();
    this.stopPromise = operation;
    const done = () => {
      if (this.stopPromise === operation) {
        this.stopPromise = undefined;
      }
    };
    void operation.then(done, () => {
      if (this.child === child) {
        this.setState(child && alive(child) ? "unresponsive" : "failed");
      }
      done();
    });
    return operation;
  }

  private request(
    method: string,
    path: string,
    body: string | undefined,
    timeoutMs: number,
  ): Promise<unknown> {
    const endpoint = this.handshake;
    if (!endpoint) {
      return Promise.reject(
        new RuntimeClientError("transport", "Runtime is not connected."),
      );
    }
    return new Promise((resolve, reject) => {
      let finished = false;
      const finish = (error?: Error, value?: unknown) => {
        if (finished) {
          return;
        }
        finished = true;
        clearTimeout(timer);
        this.requests.delete(request);
        if (error) {
          reject(error);
        } else {
          resolve(value);
        }
      };
      const request = http.request(
        {
          hostname: "127.0.0.1",
          port: endpoint.port,
          path,
          method,
          headers: {
            Authorization: `Bearer ${this.token}`,
            "Content-Type": "application/json",
            ...(body === undefined
              ? {}
              : { "Content-Length": Buffer.byteLength(body) }),
          },
          agent: false,
        },
        (response) => {
          let size = 0;
          const chunks: Buffer[] = [];
          response.on("data", (chunk: Buffer) => {
            size += chunk.length;
            if (size > MAX_RESPONSE_BYTES) {
              const error = new RuntimeClientError(
                "protocol",
                "Runtime response exceeds the 1 MiB output budget.",
              );
              finish(error);
              request.destroy(error);
            } else {
              chunks.push(chunk);
            }
          });
          response.once("aborted", () =>
            finish(
              new RuntimeClientError(
                "transport",
                "Runtime connection closed before the response completed.",
              ),
            ),
          );
          response.once("error", (error) => finish(error));
          response.once("end", () => {
            let value: unknown;
            try {
              value = JSON.parse(Buffer.concat(chunks).toString("utf8"));
            } catch {
              finish(
                new RuntimeClientError(
                  "protocol",
                  "Runtime returned invalid JSON.",
                ),
              );
              return;
            }
            if (response.statusCode !== 200) {
              const kind = response.statusCode === 409 ? "busy" : "transport";
              finish(
                new RuntimeClientError(
                  kind,
                  `Runtime HTTP ${response.statusCode}: ${
                    (value as { error?: { message?: string } })?.error
                      ?.message ?? "request failed"
                  }`,
                ),
              );
            } else {
              finish(undefined, value);
            }
          });
        },
      );
      this.requests.add(request);
      const timer = setTimeout(
        () =>
          request.destroy(
            new RuntimeClientError(
              "transport",
              "Runtime request timed out. Execution may still be running; interrupt or restart the runtime.",
            ),
          ),
        timeoutMs,
      );
      request.once("error", (error) => finish(error));
      if (body !== undefined) {
        request.write(body);
      }
      request.end();
    });
  }
}
