// A real child process/loopback socket, for protocol and lifecycle failure tests.
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const mode = process.argv[2];
const token = process.argv.find((arg) => arg.startsWith("--token=")).slice(8);
const workspace = process.argv[process.argv.indexOf("--workspace") + 1];
const version = "0.1.0";
if (mode === "exit") {
  process.stderr.write("No module named neura_runtime\n");
  process.exit(9);
}
if (mode === "hang-startup") {
  setInterval(() => {}, 1000);
  return;
}
let busy = false;
const server = http.createServer((req, res) => {
  if (req.headers.authorization !== `Bearer ${token}`) {
    res.writeHead(401);
    res.end(
      JSON.stringify({
        ok: false,
        version,
        error: { type: "Unauthorized", message: "bad token" },
      }),
    );
    return;
  }
  let body = "";
  req.on("data", (chunk) => {
    body += chunk;
  });
  req.on("end", () => {
    res.setHeader("Content-Type", "application/json");
    if (req.url === "/health") {
      res.end(
        JSON.stringify({
          ok: true,
          version,
          busy,
          status: busy ? "busy" : "ready",
        }),
      );
    } else if (req.url === "/interrupt") {
      const interrupted = busy;
      busy = false;
      res.end(JSON.stringify({ ok: true, version, interrupted }));
    } else if (req.url === "/vars") {
      res.end(
        JSON.stringify({
          ok: true,
          version,
          vars: [
            {
              name: "df",
              type: "DataFrame",
              kind: "dataframe",
              shape: [3, 2],
              preview: "   a  b",
            },
          ],
          total: 1,
          truncated: false,
        }),
      );
    } else if (req.url === "/inspect") {
      const payload = JSON.parse(body || "{}");
      if (payload.name === "missing") {
        res.end(
          JSON.stringify({
            ok: false,
            version,
            error: {
              type: "DataFrameError",
              message: "no variable named 'missing'",
            },
          }),
        );
        return;
      }
      res.end(
        JSON.stringify({
          ok: true,
          version,
          inspect: {
            name: payload.name,
            type: "DataFrame",
            shape: [3, 2],
            columns: [
              { name: "a", dtype: "int64", null_count: 0 },
              { name: "b", dtype: "object", null_count: 1 },
            ],
            rows: [[1, "x"]],
            row_count: 1,
            more_rows: true,
            truncated: false,
          },
        }),
      );
    } else if (req.url === "/profile") {
      const payload = JSON.parse(body || "{}");
      res.end(
        JSON.stringify({
          ok: true,
          version,
          profile: {
            name: payload.name,
            type: "DataFrame",
            shape: [3, 2],
            rows_scanned: 3,
            rows_sampled: false,
            partial: false,
            duration_ms: 4,
            payload_bytes: 512,
            columns: [
              {
                name: "a",
                dtype: "int64",
                null_count: 0,
                null_pct: 0,
                cardinality: 3,
                numeric: {
                  min: 1,
                  max: 3,
                  mean: 2,
                  std: 1,
                  quantiles: { "0.25": 1.5, "0.5": 2, "0.75": 2.5 },
                },
              },
            ],
            sample: null,
          },
        }),
      );
    } else if (req.url === "/last_error") {
      res.end(
        JSON.stringify({
          ok: true,
          version,
          last_error: mode === "with-error"
            ? { type: "ValueError", message: "boom", traceback: "Traceback…" }
            : null,
        }),
      );
    } else if (req.url === "/stop") {
      res.end(JSON.stringify({ ok: true, version, stopped: true }));
    } else if (req.url === "/eval") {
      const code = JSON.parse(body).code;
      fs.writeFileSync(path.join(workspace, "executing.flag"), code);
      if (mode === "oversize") {
        res.end(JSON.stringify({ output: "x".repeat(2 * 1024 * 1024) }));
        return;
      }
      if (mode === "invalid-output") {
        res.end(JSON.stringify({ ok: true, version }));
        return;
      }
      if (mode === "busy-after-timeout") {
        busy = true;
        res.end(
          JSON.stringify({
            ok: false,
            version,
            busy: true,
            error: { type: "TimeoutError", message: "still running" },
          }),
        );
        return;
      }
      if (mode === "crash") {
        process.exit(7);
      }
      if (mode === "http-error") {
        res.writeHead(409);
        res.end(
          JSON.stringify({
            ok: false,
            version,
            error: { type: "BusyError", message: "busy" },
          }),
        );
        return;
      }
      busy = true;
      const delay =
        code === "hang"
          ? 30000
          : code.startsWith("delay:")
            ? Number(code.slice(6))
            : 0;
      setTimeout(() => {
        busy = false;
        res.end(
          JSON.stringify({
            ok: true,
            version,
            execution: {
              stdout: code,
              stderr: "",
              result_repr: null,
              result_kind: "none",
              duration_ms: delay,
              stdout_truncated: false,
              stderr_truncated: false,
              result_repr_truncated: false,
              state_changed: false,
              rich_truncated: false,
              outputs: mode === "rich"
                ? [
                    {
                      kind: "html",
                      html: "<table><tr><td>captured</td></tr></table>",
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
                  ]
                : [],
            },
          }),
        );
      }, delay);
    } else {
      res.writeHead(404);
      res.end("{}");
    }
  });
});
server.listen(0, "127.0.0.1", () => {
  if (mode === "bad-handshake") {
    process.stdout.write("not JSON\n");
    return;
  }
  const line =
    JSON.stringify({
      neura_runtime: !mode.startsWith("bridge"),
      neura_bridge: mode.startsWith("bridge") ? true : undefined,
      port: server.address().port,
      pid: process.pid,
      version: mode === "bad-version" ? "9.0.0" : version,
      kernel: mode.startsWith("bridge") ? "ZMQInteractiveShell" : undefined,
      kernel_name: mode.startsWith("bridge") ? "python3" : undefined,
      connection_file: mode.startsWith("bridge")
        ? "/tmp/kernel-1234.json"
        : undefined,
    }) + "\r\n";
  if (mode === "bridge-attach") {
    // Mimic the real attach CLI: a short-lived helper prints the handshake
    // and exits, while the bridge inside the kernel keeps serving HTTP.
    const { spawn } = require("node:child_process");
    const helper = spawn(process.execPath, ["-e", `process.stdout.write(${JSON.stringify(line)})`], {
      stdio: ["ignore", "inherit", "inherit"],
    });
    helper.on("error", () => {});
    return;
  }
  if (mode === "split") {
    process.stdout.write(line.slice(0, 17));
    setTimeout(() => process.stdout.write(line.slice(17)), 10);
  } else {
    process.stdout.write(line);
  }
  if (mode === "redact") {
    process.stderr.write("secret: " + token.slice(0, 23));
    setTimeout(() => process.stderr.write(token.slice(23) + "\n"), 15);
  }
});
process.on("SIGTERM", () => {
  if (mode !== "ignore-term") {
    process.exit(0);
  }
});
