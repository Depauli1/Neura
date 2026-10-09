import * as vscode from "vscode";

export function activate(context: vscode.ExtensionContext) {
  const ids = [
    "neura.ask", "neura.runCell", "neura.runSelection",
    "neura.restartRuntime", "neura.interrupt",
    "neura.showOutputs", "neura.profileDataFrame",
  ];
  for (const id of ids) {
    context.subscriptions.push(
      vscode.commands.registerCommand(id, () =>
        vscode.window.showInformationMessage(`${id}: not implemented yet`)
      )
    );
  }
}

export function deactivate() {}
