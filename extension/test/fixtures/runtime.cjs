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
              duration_ms: delay,
              stdout_truncated: false,
              stderr_truncated: false,
              result_repr_truncated: false,
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
      neura_runtime: true,
      port: server.address().port,
      pid: process.pid,
      version: mode === "bad-version" ? "9.0.0" : version,
    }) + "\r\n";
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
