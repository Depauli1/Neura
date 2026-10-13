import { strict as assert } from "node:assert";
import { test } from "node:test";
import {
  escapeHtml,
  OutputEntry,
  plainText,
  renderOutputs,
} from "../outputView";
import { parseEval, parseHandshake, parseHealth } from "../protocol";

export const response = {
  ok: true,
  version: "0.1.0",
  execution: {
    stdout: "hello\n",
    stderr: "warning\n",
    result_repr: "42",
    duration_ms: 4,
    stdout_truncated: false,
    stderr_truncated: false,
    result_repr_truncated: false,
  },
};
test("protocol validates localhost port, PID, version and output schemas", () => {
  assert.equal(
    parseHandshake(
      JSON.stringify({
        neura_runtime: true,
        port: 51000,
        pid: 12,
        version: "0.1.0",
      }),
    ).port,
    51000,
  );
  for (const value of [
    "not json",
    { neura_runtime: true, port: 0, pid: 12, version: "0.1.0" },
    { neura_runtime: true, port: 50000, pid: -1, version: "0.1.0" },
    { neura_runtime: true, port: 50000, pid: 1, version: "0.2.0" },
  ]) {
    assert.throws(() =>
      parseHandshake(typeof value === "string" ? value : JSON.stringify(value)),
    );
  }
  assert.equal(
    parseHealth({ ok: true, version: "0.1.0", busy: true, status: "busy" })
      .busy,
    true,
  );
  assert.throws(() =>
    parseHealth({ ok: true, version: "0.1.0", busy: true, status: "ready" }),
  );
  assert.deepEqual(parseEval(response), response);
  assert.throws(() => parseEval({ ok: true, version: "0.1.0" }));
  assert.throws(() =>
    parseEval({
      ...response,
      execution: { ...response.execution, stdout: 42 },
    }),
  );
  assert.throws(() => parseEval({ ok: false, version: "0.1.0" }));
  assert.equal(
    parseEval({
      ok: false,
      version: "0.1.0",
      busy: true,
      error: { type: "TimeoutError", message: "still running" },
    }).busy,
    true,
  );
});
test("output HTML uses CSP/nonces and escapes every user-controlled field", () => {
  const injection = "<script>alert('x')</script><img src=x onerror=alert(1)>";
  const entry: OutputEntry = {
    id: "id",
    workspaceKey: "root",
    fileUri: "file:///example.py",
    fileName: injection,
    label: injection,
    startLine: 0,
    endLine: 2,
    dirty: true,
    code: injection,
    state: "error",
    response: {
      ...response,
      ok: false,
      error: { type: injection, message: injection, traceback: injection },
      execution: {
        ...response.execution,
        stdout: injection,
        stderr: injection,
        result_repr: injection,
        stdout_truncated: true,
      },
    },
  };
  const html = renderOutputs(
    [entry],
    [
      {
        key: "root",
        name: injection,
        snapshot: { state: "busy", python: injection, pending: 1 },
      },
    ],
    "root",
    "testnonce",
  );
  assert(!html.includes(injection));
  assert(html.includes("&lt;script&gt;"));
  assert(html.includes("default-src 'none'"));
  assert(html.includes("script-src 'nonce-testnonce'"));
  assert(html.includes('nonce="testnonce"'));
  assert(html.includes("stdout"));
  assert(html.includes("stderr"));
  assert(html.includes("Result"));
  assert(html.includes("unsaved buffer"));
  assert(html.includes("truncated"));
  assert.equal(escapeHtml(`&<>"'`), "&amp;&lt;&gt;&quot;&#39;");
  assert.equal(plainText("\x1b[31mred\x1b[0m"), "red");
});
test("output view filters workspaces and shows busy/empty guidance", () => {
  const entry: OutputEntry = {
    id: "a",
    workspaceKey: "other",
    fileUri: "file:///x.py",
    fileName: "x.py",
    label: "hidden entry",
    startLine: 0,
    endLine: 1,
    dirty: false,
    code: "secret",
    state: "success",
  };
  const html = renderOutputs(
    [entry],
    [
      {
        key: "root",
        name: "root",
        snapshot: { state: "stopped", python: "Not started", pending: 0 },
      },
    ],
    "root",
    "nonce",
  );
  assert(!html.includes("hidden entry"));
  assert(html.includes("Your workspace, alive."));
  assert(html.includes('data-action="interrupt" disabled'));
});
