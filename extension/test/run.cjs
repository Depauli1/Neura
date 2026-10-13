// Pass explicit files so Node 20 and Windows do not need shell glob expansion.
const { readdirSync } = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const directory = path.resolve(__dirname, "../out/test");
const files = readdirSync(directory)
  .filter((name) => name.endsWith(".test.js"))
  .sort()
  .map((name) => path.join(directory, name));
if (!files.length) {
  throw new Error("No compiled extension tests found");
}
const result = spawnSync(process.execPath, ["--test", ...files], {
  stdio: "inherit",
  shell: false,
});
if (result.error) {
  throw result.error;
}
process.exit(result.status ?? 1);
