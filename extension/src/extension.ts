import * as path from "node:path";
import * as vscode from "vscode";
import { cellAt } from "./cells";
import { OutputsPanel } from "./outputsPanel";
import { cancelled, RuntimeClientError } from "./protocol";
import { resolvePython } from "./python";
import { ManagedRuntime, RuntimeSnapshot } from "./runtime";

let shutdown: (() => Promise<void>) | undefined;

export function activate(context: vscode.ExtensionContext): void {
  const log = vscode.window.createOutputChannel("Neura Runtime");
  const status = vscode.window.createStatusBarItem(
    vscode.StatusBarAlignment.Left,
    40,
  );
  const sessions = new Map<string, Promise<ManagedRuntime>>();
  const snapshots = new Map<string, RuntimeSnapshot>();
  let disposed = false;
  const outputs = new OutputsPanel({
    restart,
    interrupt,
    log: () => log.show(true),
    report,
  });
  for (const folder of vscode.workspace.workspaceFolders ?? []) {
    outputs.addWorkspace(folder.uri.toString(), folder.name);
  }

  function report(error: unknown): void {
    if (
      disposed ||
      (error instanceof RuntimeClientError && error.kind === "cancelled")
    ) {
      return;
    }
    const text = error instanceof Error ? error.message : String(error);
    log.appendLine(text);
    void vscode.window
      .showErrorMessage(
        `Neura: ${text.slice(0, 1800)}`,
        "Runtime log",
        "Interpreter settings",
      )
      .then((action) => {
        if (disposed) {
          return;
        }
        if (action === "Runtime log") {
          log.show(true);
        }
        if (action === "Interpreter settings") {
          void vscode.commands.executeCommand(
            "workbench.action.openSettings",
            "neura.pythonPath",
          );
        }
      });
  }
  function trusted(): void {
    if (!vscode.workspace.isTrusted) {
      throw new Error(
        "Trust this workspace before executing Python code with Neura.",
      );
    }
  }
  function folders(): readonly vscode.WorkspaceFolder[] {
    return vscode.workspace.workspaceFolders ?? [];
  }
  function folderByKey(key: string): vscode.WorkspaceFolder | undefined {
    return folders().find((folder) => folder.uri.toString() === key);
  }
  function currentFolder(): vscode.WorkspaceFolder | undefined {
    const editor = vscode.window.activeTextEditor;
    return (
      (editor && vscode.workspace.getWorkspaceFolder(editor.document.uri)) ??
      (outputs.selectedWorkspaceKey
        ? folderByKey(outputs.selectedWorkspaceKey)
        : undefined) ??
      folders()[0]
    );
  }
  async function chooseFolder(
    key?: string,
  ): Promise<vscode.WorkspaceFolder | undefined> {
    trusted();
    let folder = key ? folderByKey(key) : currentFolder();
    if (!folder && folders().length > 1) {
      const chosen = await vscode.window.showQuickPick(
        folders().map((folder) => ({ label: folder.name, folder })),
        { placeHolder: "Choose a workspace runtime" },
      );
      folder = chosen?.folder;
    }
    if (!folder) {
      throw new Error(
        "Open a workspace folder before starting a Neura runtime.",
      );
    }
    if (folder.uri.scheme !== "file") {
      throw new Error(
        "V1 managed execution supports local file workspaces only.",
      );
    }
    return folder;
  }
  function setting(
    folder: vscode.WorkspaceFolder,
    name: string,
    fallback: number,
    max: number,
  ): number {
    const value = vscode.workspace
      .getConfiguration("neura", folder.uri)
      .get<number>(name, fallback);
    if (!Number.isFinite(value) || value <= 0 || value > max) {
      throw new Error(
        `neura.${name} must be greater than 0 and at most ${max}.`,
      );
    }
    return value;
  }
  function updateStatus(): void {
    if (disposed) {
      return;
    }
    const folder = currentFolder();
    const snapshot = folder && snapshots.get(folder.uri.toString());
    const state = snapshot?.state ?? "stopped";
    const busy = ["busy", "unresponsive", "starting", "stopping"].includes(
      state,
    );
    status.text = `${busy ? "$(sync~spin)" : "$(play-circle)"} Neura: ${state === "stopped" ? "Python" : state}`;
    status.command = "neura.showOutputs";
    status.tooltip = `${folder?.name ?? "No workspace"}\n${snapshot?.python ?? "Starts on first execution"}\nClick to show outputs and runtime controls.`;
    status.show();
    const editor = vscode.window.activeTextEditor;
    const canRun =
      !!editor &&
      editor.document.uri.scheme === "file" &&
      editor.document.fileName.toLowerCase().endsWith(".py");
    void vscode.commands.executeCommand(
      "setContext",
      "neura.canRun",
      canRun && vscode.workspace.isTrusted,
    );
    void vscode.commands.executeCommand(
      "setContext",
      "neura.runtimeBusy",
      busy,
    );
  }
  function session(folder: vscode.WorkspaceFolder): Promise<ManagedRuntime> {
    const key = folder.uri.toString();
    const existing = sessions.get(key);
    if (existing) {
      return existing;
    }
    const promise = (async () => {
      const python = await resolvePython(folder);
      if (disposed || !folderByKey(key)) {
        throw cancelled();
      }
      return new ManagedRuntime({
        workspace: folder.uri.fsPath,
        python,
        startupTimeoutMs:
          setting(folder, "startupTimeoutSeconds", 30, 120) * 1000,
        onLog: (text) => {
          if (!disposed) {
            log.appendLine(`[${folder.name}] ${text}`);
          }
        },
        onState: (snapshot) => {
          if (!disposed && folderByKey(key)) {
            snapshots.set(key, snapshot);
            outputs.updateRuntime(key, snapshot);
            updateStatus();
          }
        },
      });
    })();
    sessions.set(key, promise);
    void promise.catch(() => {
      if (sessions.get(key) === promise) {
        sessions.delete(key);
      }
    });
    return promise;
  }

  async function run(kind: "cell" | "selection"): Promise<void> {
    trusted();
    const editor = vscode.window.activeTextEditor;
    if (
      !editor ||
      editor.document.uri.scheme !== "file" ||
      !editor.document.fileName.toLowerCase().endsWith(".py")
    ) {
      throw new Error(
        "Open a local .py file to run a cell or selection. Notebook execution is not implemented yet.",
      );
    }
    const folder = vscode.workspace.getWorkspaceFolder(editor.document.uri);
    if (!folder) {
      throw new Error(
        "The Python file must belong to an open workspace folder.",
      );
    }
    if (folder.uri.scheme !== "file") {
      throw new Error("V1 managed execution supports local workspaces only.");
    }
    const document = editor.document;
    let code: string, startLine: number, endLine: number, label: string;
    if (kind === "cell") {
      const cell = cellAt(document.getText(), editor.selection.active.line);
      if (cell.kind === "markdown") {
        void vscode.window.showInformationMessage(
          "Neura: This is a markdown cell. Move to a Python code cell to execute.",
        );
        return;
      }
      ({ code, startLine, endLine } = cell);
      label = cell.title ?? "Python cell";
    } else if (editor.selection.isEmpty) {
      startLine = editor.selection.active.line;
      endLine = startLine + 1;
      code = document.lineAt(startLine).text.trimStart();
      label = "Current line";
    } else {
      code = document.getText(editor.selection);
      startLine = editor.selection.start.line;
      endLine =
        editor.selection.end.line +
        (editor.selection.end.character > 0 ? 1 : 0);
      label = "Selection";
    }
    if (!code.trim()) {
      void vscode.window.showInformationMessage("Neura: No code to execute.");
      return;
    }
    const timeout = setting(folder, "executionTimeoutSeconds", 30, 3600);
    const id = outputs.add({
      workspaceKey: folder.uri.toString(),
      fileUri: document.uri.toString(),
      fileName: path.basename(document.fileName),
      label,
      code,
      startLine,
      endLine,
      dirty: document.isDirty,
    });
    try {
      const runtime = await session(folder);
      const response = await runtime.run(code, timeout, () =>
        outputs.running(id),
      );
      if (!disposed) {
        outputs.complete(id, response);
      }
    } catch (error) {
      if (!disposed) {
        outputs.fail(id, error);
        report(error);
      }
    }
  }
  async function restart(key?: string): Promise<void> {
    const folder = await chooseFolder(key);
    if (!folder) {
      return;
    }
    const runtime = await session(folder);
    outputs.show();
    await vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title: `Neura: Restarting ${folder.name} runtime…`,
      },
      () => runtime.restart(() => resolvePython(folder)),
    );
    void vscode.window.showInformationMessage(
      `Neura: ${folder.name} runtime restarted. Python variables were reset.`,
    );
  }
  async function interrupt(key?: string): Promise<void> {
    const folder = await chooseFolder(key);
    if (!folder) {
      return;
    }
    const existing = sessions.get(folder.uri.toString());
    const interrupted = existing ? await (await existing).interrupt() : false;
    void vscode.window.showInformationMessage(
      interrupted
        ? "Neura: Interrupt requested."
        : "Neura: No active execution to interrupt.",
    );
  }
  function command(
    id: string,
    action: (...args: string[]) => void | Promise<void>,
  ): void {
    context.subscriptions.push(
      vscode.commands.registerCommand(id, async (...args: string[]) => {
        try {
          await action(...args);
        } catch (error) {
          report(error);
        }
      }),
    );
  }
  command("neura.runCell", () => run("cell"));
  command("neura.runSelection", () => run("selection"));
  command("neura.restartRuntime", restart);
  command("neura.interrupt", interrupt);
  command("neura.showOutputs", () => outputs.show());
  command("neura.showRuntimeLog", () => log.show(true));
  command("neura.ask", () => {
    void vscode.window.showInformationMessage(
      "Neura Chat is not implemented yet. You can run Python cells and selections now.",
    );
  });
  command("neura.profileDataFrame", () => {
    void vscode.window.showInformationMessage(
      "DataFrame profiling is a later milestone; it is not implemented yet.",
    );
  });
  context.subscriptions.push(
    vscode.window.onDidChangeActiveTextEditor(updateStatus),
  );
  context.subscriptions.push(
    vscode.workspace.onDidChangeWorkspaceFolders((event) => {
      for (const folder of event.added) {
        outputs.addWorkspace(folder.uri.toString(), folder.name);
      }
      for (const folder of event.removed) {
        const key = folder.uri.toString();
        const existing = sessions.get(key);
        sessions.delete(key);
        snapshots.delete(key);
        outputs.removeWorkspace(key);
        if (existing) {
          void existing.then((runtime) => runtime.dispose()).catch(report);
        }
      }
      updateStatus();
    }),
  );
  shutdown = async () => {
    if (disposed) {
      return;
    }
    disposed = true;
    await Promise.all(
      [...sessions.values()].map(async (promise) => {
        try {
          await (await promise).dispose();
        } catch (error) {
          log.appendLine(
            error instanceof Error ? error.message : String(error),
          );
        }
      }),
    );
    sessions.clear();
    outputs.dispose();
    status.dispose();
    log.dispose();
  };
  context.subscriptions.push({
    dispose: () => {
      void shutdown?.();
    },
  });
  updateStatus();
}

export async function deactivate(): Promise<void> {
  await shutdown?.();
  shutdown = undefined;
}
