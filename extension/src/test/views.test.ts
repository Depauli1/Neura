import { strict as assert } from "node:assert";
import { test } from "node:test";
import {
  escapeHtml,
  OutputEntry,
  plainText,
  renderOutputs,
} from "../outputView";
import {
  EvalResponse,
  ExecutionOutput,
  parseEval,
  parseHandshake,
  parseHealth,
} from "../protocol";

export const execution: ExecutionOutput = {
  stdout: "hello\n",
  stderr: "warning\n",
  result_repr: "42",
  result_kind: "value",
  duration_ms: 4,
  stdout_truncated: false,
  stderr_truncated: false,
  result_repr_truncated: false,
  state_changed: true,
  rich_truncated: false,
  outputs: [],
};
export const response: EvalResponse = {
  ok: true,
  version: "0.1.0",
  execution,
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
      execution: { ...execution, stdout: 42 },
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
        ...execution,
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

test("bridge handshakes parse with kernel details", () => {
  const handshake = parseHandshake(
    JSON.stringify({
      neura_bridge: true,
      port: 51000,
      pid: 12,
      version: "0.1.0",
      kernel: "ZMQInteractiveShell",
      kernel_name: "python3",
      connection_file: "/tmp/kernel-1.json",
    }),
  );
  assert.equal(handshake.backend, "bridge");
  assert.equal(handshake.kernelName, "python3");
  assert.equal(handshake.connectionFile, "/tmp/kernel-1.json");
  assert.throws(() =>
    parseHandshake(
      JSON.stringify({ neura_runtime: true, port: 1, pid: 1, version: "0.1.0" })
        .replace("neura_runtime", "neura_other"),
    ),
  );
});

test("rich outputs render tables, plots and sanitized HTML", () => {
  const entry: OutputEntry = {
    id: "rich",
    workspaceKey: "root",
    fileUri: "file:///example.py",
    fileName: "example.py",
    label: "cell",
    startLine: 0,
    endLine: 2,
    dirty: false,
    code: "df",
    state: "success",
    response: {
      ...response,
      execution: {
        ...execution,
        outputs: [
          {
            kind: "html",
            html: "<table><tr><td>kept</td></tr></table>",
            truncated: false,
          },
          {
            kind: "image_png",
            format: "png",
            data: "iVBORw0KGgo=",
            bytes: 8,
            width: 4,
            height: 4,
            truncated: false,
          },
          {
            kind: "dataframe",
            shape: [2, 2],
            columns: [
              { name: "a", dtype: "int64" },
              { name: "b", dtype: "object" },
            ],
            rows: [[1, "x"]],
            row_count: 1,
            truncated: false,
          },
          { kind: "text", text: "figure dropped", truncated: true },
        ],
      },
    },
  };
  const html = renderOutputs(
    [entry],
    [
      {
        key: "root",
        name: "root",
        snapshot: { state: "ready", python: "python3", pending: 0 },
      },
    ],
    "root",
    "nonce",
  );
  assert(html.includes("<td>kept</td>"));
  assert(html.includes("data:image/png;base64,iVBORw0KGgo="));
  assert(html.includes("<th>a<span class=\"dtype\">int64</span></th>"));
  assert(html.includes("<td>1</td>"));
  assert(html.includes("figure dropped"));
  assert(html.includes("img-src data:"));
});

test("captured HTML is sanitized against an allowlist", () => {
  const hostile =
    "<p onclick=\"alert(1)\">ok</p>" +
    "<script>alert(2)</script>" +
    "<a href=\"javascript:alert(3)\">bad</a>" +
    "<a href=\"https://example.com\">good</a>" +
    "<img src=\"x\" onerror=\"alert(4)\">" +
    "<img src=\"data:image/png;base64,iVBORw0KGgo=\">" +
    "<iframe src=\"https://evil.example\"></iframe>" +
    "<table style=\"color:red;position:fixed;background:url(javascript:1)\"><tr><td>cell</td></tr></table>";
  const entry: OutputEntry = {
    id: "html",
    workspaceKey: "root",
    fileUri: "file:///example.py",
    fileName: "example.py",
    label: "cell",
    startLine: 0,
    endLine: 1,
    dirty: false,
    code: "display(HTML(...))",
    state: "success",
    response: {
      ...response,
      execution: {
        ...execution,
        outputs: [{ kind: "html", html: hostile, truncated: false }],
      },
    },
  };
  const html = renderOutputs(
    [entry],
    [
      {
        key: "root",
        name: "root",
        snapshot: { state: "ready", python: "python3", pending: 0 },
      },
    ],
    "root",
    "nonce",
  );
  // Kept: allowlisted markup, safe URL, safe data: image, safe style rule.
  assert(html.includes("<p>ok</p>"));
  assert(html.includes('<a href="https://example.com">good</a>'));
  assert(html.includes("<td>cell</td>"));
  assert(html.includes("color:red"));
  // Dropped: handlers, scripts, javascript: URLs, unsafe attrs, iframes.
  // (The page's own nonce'd <script> tag is expected and unrelated.)
  assert(!html.includes("onclick"));
  assert(!html.includes("onerror"));
  assert(!html.includes("<script>alert(2)"));
  assert(!html.includes("alert(2)"));
  assert(!html.includes("javascript:"));
  assert(!html.includes("<iframe"));
  assert(!html.includes("evil.example"));
  assert(!html.includes("position:fixed"));
  assert(!html.includes('src="x"'));
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
