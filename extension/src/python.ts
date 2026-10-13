import * as path from "node:path";
import * as vscode from "vscode";
import { PythonCommand } from "./runtime";

interface PythonApi {
  ready?: Promise<void>;
  environments?: {
    getActiveEnvironmentPath(resource?: vscode.Uri): {
      path: string;
      id: string;
    };
    resolveEnvironment(
      environment: string,
    ): Promise<{ executable?: { uri?: vscode.Uri } } | undefined>;
  };
  settings?: {
    getExecutionDetails(resource?: vscode.Uri): { execCommand?: string[] };
  };
}

export async function resolvePython(
  folder: vscode.WorkspaceFolder,
): Promise<PythonCommand> {
  const configured = vscode.workspace
    .getConfiguration("neura", folder.uri)
    .get<string>("pythonPath", "")
    .trim();
  if (configured) {
    // A literal executable path/command, never a shell command. Keep spaces intact.
    const command =
      !path.isAbsolute(configured) && /[\\/]/.test(configured)
        ? path.resolve(folder.uri.fsPath, configured)
        : configured;
    return { command };
  }
  const extension =
    vscode.extensions.getExtension<PythonApi>("ms-python.python");
  if (extension) {
    let timer: NodeJS.Timeout | undefined;
    try {
      const resolve = (async (): Promise<PythonCommand> => {
        const api = await extension.activate();
        await api.ready;
        if (api.environments) {
          const active = api.environments.getActiveEnvironmentPath(folder.uri);
          if (active?.id || active?.path) {
            const environment = await api.environments.resolveEnvironment(
              active.id || active.path,
            );
            if (environment?.executable?.uri?.fsPath) {
              return { command: environment.executable.uri.fsPath };
            }
          }
        }
        const command = api.settings?.getExecutionDetails(
          folder.uri,
        ).execCommand;
        if (
          command?.length &&
          command.every((part) => typeof part === "string" && part.length > 0)
        ) {
          return { command: command[0], args: command.slice(1) };
        }
        throw new Error("No executable Python interpreter is selected");
      })();
      return await Promise.race([
        resolve,
        new Promise<never>((_, reject) => {
          timer = setTimeout(
            () => reject(new Error("Python interpreter resolution timed out")),
            10000,
          );
        }),
      ]);
    } catch (error) {
      // Do not silently switch to a different environment if selection failed.
      throw new Error(
        `Could not resolve the selected Python interpreter: ${error instanceof Error ? error.message : String(error)}. Select an interpreter in the Python extension or set neura.pythonPath.`,
      );
    } finally {
      clearTimeout(timer);
    }
  }
  return { command: process.platform === "win32" ? "python" : "python3" };
}
