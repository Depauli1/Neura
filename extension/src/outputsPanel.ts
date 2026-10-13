import { randomBytes, randomUUID } from "node:crypto";
import * as vscode from "vscode";
import { EvalResponse, RuntimeClientError } from "./protocol";
import { OutputEntry, renderOutputs, WorkspaceView } from "./outputView";
import { RuntimeSnapshot } from "./runtime";

export class OutputsPanel implements vscode.Disposable {
  private panel?: vscode.WebviewPanel;
  private entries: OutputEntry[] = [];
  private workspaces = new Map<string, WorkspaceView>();
  private selectedKey?: string;
  private disposed = false;

  constructor(
    private readonly actions: {
      restart: (key: string) => Promise<void>;
      interrupt: (key: string) => Promise<void>;
      log: () => void;
      report: (error: unknown) => void;
    },
  ) {}
  get selectedWorkspaceKey(): string | undefined {
    return this.selectedKey;
  }

  addWorkspace(key: string, name: string): void {
    if (!this.workspaces.has(key)) {
      this.workspaces.set(key, {
        key,
        name,
        snapshot: { state: "stopped", python: "Not started", pending: 0 },
      });
    }
    this.selectedKey ??= key;
    this.render();
  }
  removeWorkspace(key: string): void {
    this.workspaces.delete(key);
    this.entries = this.entries.filter((entry) => entry.workspaceKey !== key);
    if (this.selectedKey === key) {
      this.selectedKey = this.workspaces.keys().next().value;
    }
    this.render();
  }
  updateRuntime(key: string, snapshot: RuntimeSnapshot): void {
    const workspace = this.workspaces.get(key);
    if (workspace) {
      workspace.snapshot = snapshot;
      this.render();
    }
  }
  add(entry: Omit<OutputEntry, "id" | "state">): string {
    const id = randomUUID();
    this.entries.push({
      ...entry,
      id,
      state: "queued",
      code:
        entry.code.length > 16000
          ? entry.code.slice(0, 16000) + "\n… [code display truncated]"
          : entry.code,
    });
    this.entries = this.entries.slice(-50);
    this.selectedKey = entry.workspaceKey;
    this.show();
    this.render();
    return id;
  }
  running(id: string): void {
    const entry = this.entries.find((entry) => entry.id === id);
    if (entry) {
      entry.state = "running";
      this.render();
    }
  }
  complete(id: string, response: EvalResponse): void {
    const entry = this.entries.find((entry) => entry.id === id);
    if (entry) {
      entry.response = response;
      entry.state = response.ok ? "success" : "error";
      this.render();
    }
  }
  fail(id: string, error: unknown): void {
    const entry = this.entries.find((entry) => entry.id === id);
    if (entry) {
      entry.failure = error instanceof Error ? error.message : String(error);
      entry.state =
        error instanceof RuntimeClientError && error.kind === "cancelled"
          ? "cancelled"
          : "error";
      this.render();
    }
  }
  show(): void {
    if (this.disposed) {
      return;
    }
    if (this.panel) {
      this.panel.reveal(vscode.ViewColumn.Beside, true);
      return;
    }
    this.panel = vscode.window.createWebviewPanel(
      "neura.outputs",
      "Neura Outputs",
      {
        viewColumn: vscode.ViewColumn.Beside,
        preserveFocus: true,
      },
      {
        enableScripts: true,
        retainContextWhenHidden: true,
        localResourceRoots: [],
      },
    );
    const panel = this.panel;
    panel.onDidDispose(() => {
      if (this.panel === panel) {
        this.panel = undefined;
      }
    });
    panel.webview.onDidReceiveMessage(async (message: unknown) => {
      if (!message || typeof message !== "object") {
        return;
      }
      const { type, workspaceKey, id } = message as Record<string, unknown>;
      try {
        if (type === "log") {
          this.actions.log();
          return;
        }
        if (
          typeof workspaceKey !== "string" ||
          !this.workspaces.has(workspaceKey)
        ) {
          return;
        }
        if (type === "selectWorkspace") {
          this.selectedKey = workspaceKey;
          this.render();
        } else if (type === "clear") {
          this.entries = this.entries.filter(
            (entry) => entry.workspaceKey !== workspaceKey,
          );
          this.render();
        } else if (type === "restart") {
          await this.actions.restart(workspaceKey);
        } else if (type === "interrupt") {
          await this.actions.interrupt(workspaceKey);
        } else if (type === "source" && typeof id === "string") {
          const entry = this.entries.find(
            (entry) => entry.id === id && entry.workspaceKey === workspaceKey,
          );
          if (entry) {
            const document = await vscode.workspace.openTextDocument(
              vscode.Uri.parse(entry.fileUri),
            );
            const line = Math.min(entry.startLine, document.lineCount - 1);
            await vscode.window.showTextDocument(document, {
              selection: new vscode.Range(line, 0, line, 0),
              preserveFocus: false,
            });
          }
        }
      } catch (error) {
        this.actions.report(error);
      }
    });
    this.render();
  }
  private render(): void {
    if (this.disposed || !this.panel) {
      return;
    }
    this.panel.webview.html = renderOutputs(
      this.entries,
      [...this.workspaces.values()],
      this.selectedKey,
      randomBytes(18).toString("base64"),
    );
  }
  dispose(): void {
    this.disposed = true;
    this.panel?.dispose();
    this.entries = [];
  }
}
