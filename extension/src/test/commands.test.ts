/** Exercise the actual activation/command handlers with a small VS Code host fake.
 * Process/HTTP behavior is tested separately against real child processes.
 */
import { strict as assert } from "node:assert";
import * as path from "node:path";
import { test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";
import { RuntimeOptions, RuntimeSnapshot } from "../runtime";

const commands = new Map<string, (...args: string[]) => Promise<void>>();
const errors: string[] = [],
  infos: string[] = [];
const panels: Array<{
  webview: {
    html: string;
    onDidReceiveMessage: (
      callback: (value: unknown) => Promise<void>,
    ) => unknown;
  };
  receive?: (value: unknown) => Promise<void>;
  options: unknown;
}> = [];
const config: Record<string, unknown> = {
  pythonPath: "a python executable with spaces",
  executionTimeoutSeconds: 7,
};
let pythonApi: unknown;
const uri = (file: string) => ({
  scheme: "file",
  fsPath: file,
  toString: () => pathToFileURL(file).toString(),
});
const root = { name: "demo", uri: uri(path.resolve("workspace")), index: 0 };
let folders = [root];
const disposable = { dispose() {} };
let editor: ReturnType<typeof makeEditor> | undefined;
let trusted = true;
const opened: string[] = [];
function makeEditor(
  text: string,
  startLine = 0,
  startCharacter = 0,
  endLine = startLine,
  endCharacter = startCharacter,
) {
  const lines = text.split("\n");
  const selection = {
    active: { line: startLine, character: startCharacter },
    start: { line: startLine, character: startCharacter },
    end: { line: endLine, character: endCharacter },
    isEmpty: startLine === endLine && startCharacter === endCharacter,
  };
  return {
    selection,
    document: {
      uri: uri(path.join(root.uri.fsPath, "demo.py")),
      fileName: path.join(root.uri.fsPath, "demo.py"),
      isDirty: true,
      lineCount: lines.length,
      getText: (range?: typeof selection) => {
        if (!range) {
          return text;
        }
        const offset = (line: number, char: number) =>
          lines.slice(0, line).reduce((n, value) => n + value.length + 1, 0) +
          char;
        return text.slice(
          offset(range.start.line, range.start.character),
          offset(range.end.line, range.end.character),
        );
      },
      lineAt: (line: number) => ({ text: lines[line] }),
    },
  };
}
class RuntimeFake {
  static instances: RuntimeFake[] = [];
  runs: Array<{ code: string; timeout: number }> = [];
  restarts = 0;
  interrupts = 0;
  disposals = 0;
  constructor(readonly options: RuntimeOptions) {
    RuntimeFake.instances.push(this);
  }
  state(state: RuntimeSnapshot["state"]) {
    this.options.onState?.({
      state,
      python: this.options.python.command,
      pending: 0,
    });
  }
  async run(code: string, timeout: number, onStarted?: () => void) {
    this.runs.push({ code, timeout });
    this.state("busy");
    onStarted?.();
    this.state("ready");
    return {
      ok: true,
      version: "0.1.0",
      execution: {
        stdout: `Ran ${code}`,
        stderr: "",
        result_repr: null,
        duration_ms: 1,
        stdout_truncated: false,
        stderr_truncated: false,
        result_repr_truncated: false,
      },
    };
  }
  async restart(resolve?: () => Promise<unknown>) {
    if (resolve) {
      await resolve();
    }
    this.restarts++;
    this.state("ready");
  }
  async interrupt() {
    this.interrupts++;
    return true;
  }
  async dispose() {
    this.disposals++;
  }
}
const vscode = {
  Uri: { parse: (value: string) => uri(fileURLToPath(value)) },
  Range: class {
    constructor(
      public startLine: number,
      public startCharacter: number,
      public endLine: number,
      public endCharacter: number,
    ) {}
  },
  ViewColumn: { Beside: 2 },
  StatusBarAlignment: { Left: 1 },
  ProgressLocation: { Notification: 15 },
  commands: {
    registerCommand: (
      id: string,
      callback: (...args: string[]) => Promise<void>,
    ) => {
      commands.set(id, callback);
      return disposable;
    },
    executeCommand: async () => {},
  },
  workspace: {
    get isTrusted() {
      return trusted;
    },
    get workspaceFolders() {
      return folders;
    },
    getWorkspaceFolder: (value: { fsPath: string }) =>
      folders.find((folder) =>
        value.fsPath.startsWith(folder.uri.fsPath + path.sep),
      ),
    getConfiguration: () => ({
      get: (key: string, fallback: unknown) => config[key] ?? fallback,
    }),
    onDidChangeWorkspaceFolders: () => disposable,
    openTextDocument: async (value: { toString(): string }) => {
      opened.push(value.toString());
      return editor!.document;
    },
  },
  extensions: {
    getExtension: () =>
      pythonApi ? { activate: async () => pythonApi } : undefined,
  },
  window: {
    get activeTextEditor() {
      return editor;
    },
    createOutputChannel: () => ({ appendLine() {}, show() {}, dispose() {} }),
    createStatusBarItem: () => ({
      text: "",
      tooltip: "",
      command: "",
      show() {},
      dispose() {},
    }),
    showErrorMessage: async (text: string) => {
      errors.push(text);
    },
    showInformationMessage: async (text: string) => {
      infos.push(text);
    },
    showQuickPick: async () => undefined,
    withProgress: async (_: unknown, task: () => Promise<void>) => task(),
    onDidChangeActiveTextEditor: () => disposable,
    showTextDocument: async () => {},
    createWebviewPanel: (
      _type: string,
      _title: string,
      _column: unknown,
      options: unknown,
    ) => {
      const panel = {
        options,
        receive: undefined as ((value: unknown) => Promise<void>) | undefined,
        webview: {
          html: "",
          onDidReceiveMessage(callback: (value: unknown) => Promise<void>) {
            panel.receive = callback;
            return disposable;
          },
        },
        reveal() {},
        onDidDispose: () => disposable,
        dispose() {},
      };
      panels.push(panel);
      return panel;
    },
  },
};
// Intercept only these module imports while loading production modules. No VS
// Code package is available in ordinary Node; its extension host provides it.
const loader = require("node:module") as {
  _load: (id: string, parent?: NodeModule, ...args: unknown[]) => unknown;
};
const original = loader._load;
let extension: typeof import("../extension");
let resolvePython: typeof import("../python").resolvePython;
try {
  loader._load = function (id, parent, ...args) {
    if (id === "vscode") {
      return vscode;
    }
    if (
      id === "./runtime" &&
      parent?.filename.endsWith(`${path.sep}extension.js`)
    ) {
      return { ManagedRuntime: RuntimeFake };
    }
    return original.call(this, id, parent, ...args);
  };
  extension = require("../extension");
  resolvePython = require("../python").resolvePython;
} finally {
  loader._load = original;
}
function activate() {
  commands.clear();
  errors.length = 0;
  infos.length = 0;
  panels.length = 0;
  opened.length = 0;
  RuntimeFake.instances = [];
  folders = [root];
  trusted = true;
  config.pythonPath = "a python executable with spaces";
  pythonApi = undefined;
  editor = makeEditor("# %% first\nx=40\n# %% second\nx+2", 2);
  extension.activate({
    subscriptions: [],
  } as unknown as import("vscode").ExtensionContext);
}
const invoke = (command: string) => commands.get(command)!();

test("actual run-cell/selection/current-line commands use the buffer and reuse the workspace runtime", async () => {
  activate();
  try {
    await invoke("neura.runCell");
    assert.equal(RuntimeFake.instances.length, 1);
    const runtime = RuntimeFake.instances[0];
    assert.deepEqual(runtime.runs[0], { code: "x+2", timeout: 7 });
    assert.equal(runtime.options.workspace, root.uri.fsPath);
    assert.equal(
      runtime.options.python.command,
      "a python executable with spaces",
    );
    editor = makeEditor("value = 123\nprint(value)", 0, 8, 0, 11);
    await invoke("neura.runSelection");
    assert.equal(runtime.runs[1].code, "123");
    editor = makeEditor("    print('line')", 0);
    await invoke("neura.runSelection");
    assert.equal(runtime.runs[2].code, "print('line')");
    assert.equal(RuntimeFake.instances.length, 1);
    const html = panels[0].webview.html;
    assert(html.includes("Ran x+2"));
    assert(html.includes("unsaved buffer"));
    assert(html.includes("success"));
    assert.deepEqual(panels[0].options, {
      enableScripts: true,
      retainContextWhenHidden: true,
      localResourceRoots: [],
    });
    assert.equal(errors.length, 0);
  } finally {
    await extension.deactivate();
  }
  assert.equal(RuntimeFake.instances[0].disposals, 1);
});

test("restart/interrupt commands and webview toolbar dispatch to the correct session", async () => {
  activate();
  try {
    await invoke("neura.runCell");
    const runtime = RuntimeFake.instances[0];
    await invoke("neura.interrupt");
    await invoke("neura.restartRuntime");
    assert.equal(runtime.interrupts, 1);
    assert.equal(runtime.restarts, 1);
    await panels[0].receive!({
      type: "interrupt",
      workspaceKey: root.uri.toString(),
    });
    await panels[0].receive!({
      type: "restart",
      workspaceKey: root.uri.toString(),
    });
    assert.equal(runtime.interrupts, 2);
    assert.equal(runtime.restarts, 2);
    await panels[0].receive!({
      type: "restart",
      workspaceKey: "file:///unowned",
    });
    assert.equal(runtime.restarts, 2);
    assert(infos.some((message) => message.includes("variables were reset")));
  } finally {
    await extension.deactivate();
  }
});

test("trust, missing editor/folder, non-py files, markdown and empty code do not spawn", async () => {
  activate();
  try {
    trusted = false;
    await invoke("neura.runCell");
    assert(errors.pop()?.includes("Trust this workspace"));
    trusted = true;
    editor = undefined;
    await invoke("neura.runCell");
    assert(errors.pop()?.includes("Open a local .py"));
    editor = makeEditor("pass");
    editor.document.fileName = "other.txt";
    await invoke("neura.runCell");
    assert(errors.pop()?.includes("Open a local .py"));
    editor = makeEditor("pass");
    folders = [];
    await invoke("neura.runCell");
    assert(errors.pop()?.includes("open workspace folder"));
    folders = [root];
    editor = makeEditor("# %% [markdown]\n# notes");
    await invoke("neura.runCell");
    editor = makeEditor(" ");
    await invoke("neura.runSelection");
    assert.equal(RuntimeFake.instances.length, 0);
    assert(infos.some((message) => message.includes("markdown")));
    assert(infos.some((message) => message.includes("No code")));
  } finally {
    await extension.deactivate();
  }
});

test("Python selection uses the exported environment API, legacy command arrays, then PATH only when absent", async () => {
  config.pythonPath = "";
  let resource: unknown;
  pythonApi = {
    ready: Promise.resolve(),
    environments: {
      getActiveEnvironmentPath(value: unknown) {
        resource = value;
        return { id: "env-id", path: "env-path" };
      },
      async resolveEnvironment(id: string) {
        assert.equal(id, "env-id");
        return { executable: { uri: uri("/selected/python") } };
      },
    },
  };
  assert.equal(
    (await resolvePython(root as unknown as import("vscode").WorkspaceFolder))
      .command,
    "/selected/python",
  );
  assert.equal(resource, root.uri);
  pythonApi = {
    settings: {
      getExecutionDetails() {
        return { execCommand: ["conda", "run", "python"] };
      },
    },
  };
  assert.deepEqual(
    await resolvePython(root as unknown as import("vscode").WorkspaceFolder),
    { command: "conda", args: ["run", "python"] },
  );
  pythonApi = {
    environments: {
      getActiveEnvironmentPath() {
        throw new Error("bad selection");
      },
    },
  };
  await assert.rejects(
    resolvePython(root as unknown as import("vscode").WorkspaceFolder),
    /bad selection/,
  );
  pythonApi = undefined;
  assert.equal(
    (await resolvePython(root as unknown as import("vscode").WorkspaceFolder))
      .command,
    process.platform === "win32" ? "python" : "python3",
  );
  config.pythonPath = "./.venv/python";
  assert.equal(
    (await resolvePython(root as unknown as import("vscode").WorkspaceFolder))
      .command,
    path.join(root.uri.fsPath, ".venv", "python"),
  );
});
