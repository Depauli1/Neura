import { strict as assert } from "node:assert";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";
import { cellAt } from "../cells";
import { RuntimeClientError } from "../protocol";
import { AttachedRuntime, ManagedRuntime } from "../runtime";

const fakeScript = path.resolve(__dirname, "../../test/fixtures/runtime.cjs");
async function workspace(): Promise<string> {
  return mkdtemp(path.join(os.tmpdir(), "neura workspace "));
}
async function until(
  condition: () => boolean | Promise<boolean>,
  ms = 5000,
): Promise<void> {
  const deadline = Date.now() + ms;
  while (Date.now() < deadline) {
    if (await condition()) {
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 15));
  }
  throw new Error("Condition did not become true");
}
async function exists(file: string): Promise<boolean> {
  try {
    await readFile(file);
    return true;
  } catch {
    return false;
  }
}
function fake(
  root: string,
  mode = "normal",
  logs?: string[],
  timeout = 2000,
): ManagedRuntime {
  return new ManagedRuntime({
    workspace: root,
    python: { command: process.execPath, args: [fakeScript, mode] },
    startupTimeoutMs: timeout,
    onLog: (line) => logs?.push(line),
  });
}
function kind(expected: string): (error: unknown) => boolean {
  return (error) =>
    error instanceof RuntimeClientError && error.kind === expected;
}

test("split startup handshake, authenticated requests, one spawn, and serialized executions", async () => {
  const root = await workspace();
  const runtime = fake(root, "split");
  try {
    await Promise.all([runtime.start(), runtime.start()]);
    const pid = runtime.snapshot.pid;
    assert(pid);
    assert.equal(runtime.snapshot.state, "ready");
    const first = runtime.run("delay:150", 2);
    const second = runtime.run("second", 2);
    const [one, two] = await Promise.all([first, second]);
    assert.equal(one.execution?.stdout, "delay:150");
    assert.equal(two.execution?.stdout, "second");
    assert.equal(runtime.snapshot.pid, pid);
    assert.equal(runtime.snapshot.pending, 0);
    assert.equal(await runtime.interrupt(), false);
    await runtime.stop();
    assert.equal(runtime.snapshot.state, "stopped");
    assert.throws(() => process.kill(pid, 0));
  } finally {
    await runtime.dispose();
    await rm(root, { recursive: true, force: true });
  }
});

test("restart cancels active and queued runs, uses a fresh PID, and runs new work", async () => {
  const root = await workspace();
  const runtime = fake(root);
  try {
    await runtime.start();
    const oldPid = runtime.snapshot.pid!;
    const active = runtime.run("hang", 60);
    const queued = runtime.run("must-not-run", 2);
    const rejected = [
      assert.rejects(active, kind("cancelled")),
      assert.rejects(queued, kind("cancelled")),
    ];
    await until(() => exists(path.join(root, "executing.flag")));
    await runtime.restart();
    await Promise.all(rejected);
    assert.notEqual(runtime.snapshot.pid, oldPid);
    assert.throws(() => process.kill(oldPid, 0));
    assert.equal(
      await readFile(path.join(root, "executing.flag"), "utf8"),
      "hang",
    );
    assert.equal((await runtime.run("fresh", 2)).execution?.stdout, "fresh");
  } finally {
    await runtime.dispose();
    await rm(root, { recursive: true, force: true });
  }
});

test("stop during startup cancels readiness and can be started again", async () => {
  const root = await workspace();
  const runtime = fake(root, "hang-startup", undefined, 5000);
  try {
    const start = runtime.start();
    const rejected = assert.rejects(start, kind("cancelled"));
    await runtime.stop();
    await rejected;
    await runtime.restart({
      command: process.execPath,
      args: [fakeScript, "normal"],
    });
    assert.equal((await runtime.run("ok", 2)).ok, true);
  } finally {
    await runtime.dispose();
    await rm(root, { recursive: true, force: true });
  }
});

for (const [mode, errorKind] of [
  ["exit", "startup"],
  ["bad-handshake", "protocol"],
  ["bad-version", "protocol"],
  ["hang-startup", "startup"],
]) {
  test(`startup failure (${mode}) cleans up the process and reports a useful error`, async () => {
    const root = await workspace();
    const runtime = fake(
      root,
      mode,
      undefined,
      mode === "hang-startup" ? 200 : 4000,
    );
    try {
      await assert.rejects(runtime.start(), kind(errorKind));
      assert.equal(runtime.snapshot.state, "failed");
    } finally {
      await runtime.dispose();
      await rm(root, { recursive: true, force: true });
    }
  });
}

test("startup stderr redacts tokens even across chunk boundaries", async () => {
  const root = await workspace();
  const logs: string[] = [];
  const runtime = fake(root, "redact", logs);
  try {
    await runtime.start();
    await until(() => logs.join("\n").includes("[redacted]"));
    assert(!/[a-f0-9]{64}/.test(logs.join("\n")));
  } finally {
    await runtime.dispose();
    await rm(root, { recursive: true, force: true });
  }
});

test("busy-after-timeout blocks further code and interrupt/poll can recover", async () => {
  const root = await workspace();
  const runtime = fake(root, "busy-after-timeout");
  try {
    assert.equal((await runtime.run("first", 1)).busy, true);
    assert.equal(runtime.snapshot.state, "busy");
    await assert.rejects(runtime.run("should-not-run", 1), kind("busy"));
    assert.equal(
      await readFile(path.join(root, "executing.flag"), "utf8"),
      "first",
    );
    assert.equal(await runtime.interrupt(), true);
    await until(() => runtime.snapshot.state === "ready");
  } finally {
    await runtime.dispose();
    await rm(root, { recursive: true, force: true });
  }
});

for (const mode of ["oversize", "invalid-output", "crash", "http-error"]) {
  test(`HTTP/client failures (${mode}) reject without wedging the execution queue`, async () => {
    const root = await workspace();
    const runtime = fake(root, mode);
    try {
      await assert.rejects(runtime.run("code", 2));
      assert.equal(runtime.snapshot.pending, 0);
    } finally {
      await runtime.dispose();
      await rm(root, { recursive: true, force: true });
    }
  });
}

test("unexpected process exit cancels older queued work instead of replaying it in a new session", async () => {
  const root = await workspace();
  const runtime = fake(root, "crash");
  try {
    await runtime.start();
    const active = runtime.run("crash-now", 2);
    const queued = runtime.run("must-not-run", 2);
    await Promise.all([
      assert.rejects(active),
      assert.rejects(queued, kind("cancelled")),
    ]);
    assert.equal(
      await readFile(path.join(root, "executing.flag"), "utf8"),
      "crash-now",
    );
    assert.equal(runtime.snapshot.state, "failed");
    assert.equal(runtime.snapshot.pending, 0);
    await runtime.start();
    assert.equal(runtime.snapshot.state, "ready");
  } finally {
    await runtime.dispose();
    await rm(root, { recursive: true, force: true });
  }
});

test("invalid/oversized requests and disposed sessions never start a process", async () => {
  const root = await workspace();
  const runtime = fake(root);
  try {
    for (const timeout of [-1, 0, 3601, NaN, Infinity]) {
      await assert.rejects(runtime.run("pass", timeout), kind("protocol"));
    }
    await assert.rejects(
      runtime.run("x".repeat(1024 * 1024), 1),
      kind("protocol"),
    );
    assert.equal(runtime.snapshot.state, "stopped");
    await runtime.dispose();
    await assert.rejects(runtime.run("pass", 1), kind("cancelled"));
    await assert.rejects(runtime.start(), kind("cancelled"));
  } finally {
    await runtime.dispose();
    await rm(root, { recursive: true, force: true });
  }
});

test(
  "failed stop never permits a second live runtime",
  { skip: process.platform === "win32" },
  async () => {
    const root = await workspace();
    const runtime = fake(root);
    const originalKill = process.kill;
    try {
      await runtime.start();
      const pid = runtime.snapshot.pid!;
      process.kill = ((target: number, signal?: string | number) => {
        if (target === -pid && signal === "SIGTERM") {
          throw Object.assign(new Error("permission denied"), {
            code: "EPERM",
          });
        }
        return originalKill(target, signal);
      }) as typeof process.kill;
      await assert.rejects(runtime.restart(), /permission denied/);
      await assert.rejects(runtime.start(), /refusing to launch a second/);
      assert.equal(runtime.snapshot.state, "unresponsive");
      process.kill = originalKill;
      await runtime.restart();
      assert.equal(runtime.snapshot.state, "ready");
    } finally {
      process.kill = originalKill;
      await runtime.dispose();
      await rm(root, { recursive: true, force: true });
    }
  },
);

test(
  "uncooperative process is force-stopped instead of orphaned",
  { timeout: 8000 },
  async () => {
    const root = await workspace();
    const runtime = fake(root, "ignore-term");
    try {
      await runtime.start();
      const pid = runtime.snapshot.pid!;
      await runtime.stop();
      assert.throws(() => process.kill(pid, 0));
    } finally {
      await runtime.dispose();
      await rm(root, { recursive: true, force: true });
    }
  },
);

test(
  "end-to-end with the REAL Python runtime: cells, state, outputs, interrupt, restart",
  { timeout: 45000 },
  async () => {
    const root = await workspace();
    const runtime = new ManagedRuntime({
      workspace: root,
      python: {
        command:
          process.env.NEURA_TEST_PYTHON ||
          (process.platform === "win32" ? "python" : "python3"),
      },
    });
    try {
      await writeFile(path.join(root, "local_helper.py"), "answer = 23\n");
      const script = "# %% setup\nx = 40\nprint('hello')\n# %% result\nx + 2";
      const setup = await runtime.run(cellAt(script, 0).code, 5);
      assert.equal(setup.execution?.stdout, "hello\n");
      const result = await runtime.run(cellAt(script, 4).code, 5);
      assert.equal(result.execution?.result_repr, "42");
      const streams = await runtime.run(
        "import sys; print('warn', file=sys.stderr); x += 1; x",
        5,
      );
      assert.equal(streams.execution?.stderr, "warn\n");
      assert.equal(streams.execution?.result_repr, "41");
      const error = await runtime.run("print('partial'); 1/0", 5);
      assert.equal(error.ok, false);
      assert.equal(error.error?.type, "ZeroDivisionError");
      assert.equal(error.execution?.stdout, "partial\n");
      assert(error.error?.traceback?.includes("division by zero"));
      assert.equal(
        (await runtime.run("import local_helper; local_helper.answer", 5))
          .execution?.result_repr,
        "23",
      );
      const [one, two] = await Promise.all([
        runtime.run("import time; time.sleep(0.1); x += 1; x", 5),
        runtime.run("x += 1; x", 5),
      ]);
      assert.equal(one.execution?.result_repr, "42");
      assert.equal(two.execution?.result_repr, "43");
      const sleeping = runtime.run(
        "from pathlib import Path; Path('sleeping.flag').touch(); time.sleep(10)",
        15,
      );
      await until(() => exists(path.join(root, "sleeping.flag")));
      assert.equal(await runtime.interrupt(), true);
      assert.equal((await sleeping).error?.type, "KeyboardInterrupt");
      const timed = await runtime.run("time.sleep(0.6)", 0.1);
      assert.equal(timed.error?.type, "TimeoutError");
      const pid = runtime.snapshot.pid;
      await runtime.restart();
      assert.notEqual(runtime.snapshot.pid, pid);
      assert.equal(
        (await runtime.run("'x' in globals()", 5)).execution?.result_repr,
        "False",
      );
      assert.equal((await runtime.run("2 + 2", 5)).execution?.result_repr, "4");
    } finally {
      await runtime.dispose();
      await rm(root, { recursive: true, force: true });
    }
  },
);

test(
  "attached runtime: helper exits after handshake, inspection surface, stop keeps the bridge contract",
  { timeout: 45000 },
  async () => {
    const root = await workspace();
    const logs: string[] = [];
    const runtime = new AttachedRuntime({
      workspace: root,
      python: { command: process.execPath, args: [fakeScript, "bridge-attach"] },
      startupTimeoutMs: 8000,
      onLog: (line) => logs.push(line),
    });
    try {
      await runtime.start();
      assert.equal(runtime.snapshot.state, "ready");
      assert.equal(runtime.snapshot.backend, "bridge");
      assert.equal(runtime.snapshot.kernelName, "python3");
      // The helper process exited right after the handshake, by design.
      assert(await until(() => logs.length > 0).then(() => true, () => false));
      // The shared HTTP surface still answers.
      const health = await runtime.health();
      assert.equal(health.status, "ready");
      const vars = await runtime.listVars();
      assert.equal(vars.vars[0].name, "df");
      const inspect = await runtime.inspect("df", 1);
      assert.equal(inspect.inspect?.shape[0], 3);
      assert.equal(inspect.inspect?.more_rows, true);
      const missing = await runtime.inspect("missing");
      assert.equal(missing.ok, false);
      assert.equal(missing.error?.type, "DataFrameError");
      const profile = await runtime.profile("df");
      assert.equal(profile.profile?.columns[0].numeric?.mean, 2);
      assert.equal(await runtime.lastError(), null);
      const run = await runtime.run("print('from the kernel')", 10);
      assert.equal(run.execution?.stdout, "print('from the kernel')");
      // Stopping asks the bridge to stop and reports the session as stopped.
      await runtime.stop();
      assert.equal(runtime.snapshot.state, "stopped");
      // Attaching again brings the same session back without a new kernel.
      await runtime.start();
      assert.equal(runtime.snapshot.state, "ready");
      assert.equal((await runtime.health()).status, "ready");
      assert.equal(
        (await runtime.run("print('re-attached')", 10)).execution?.stdout,
        "print('re-attached')",
      );
    } finally {
      await runtime.dispose();
      await rm(root, { recursive: true, force: true });
    }
  },
);

test("managed runtime inspection surface against the fake backend", async () => {
  const root = await workspace();
  const runtime = fake(root, "normal");
  try {
    await runtime.start();
    const vars = await runtime.listVars();
    assert.equal(vars.total, 1);
    assert.equal(vars.truncated, false);
    assert.equal(vars.vars[0].kind, "dataframe");
    const inspect = await runtime.inspect("df", 2);
    assert.equal(inspect.ok, true);
    assert.deepEqual(inspect.inspect?.columns[1], {
      name: "b",
      dtype: "object",
      null_count: 1,
    });
    const missing = await runtime.inspect("missing");
    assert.equal(missing.ok, false);
    const profile = await runtime.profile("df", { sample_rows: 2 });
    assert.equal(profile.profile?.partial, false);
    assert.equal(profile.profile?.payload_bytes, 512);
    assert.equal(await runtime.lastError(), null);
  } finally {
    await runtime.dispose();
    await rm(root, { recursive: true, force: true });
  }
});
