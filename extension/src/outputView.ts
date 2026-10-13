import { EvalResponse } from "./protocol";
import { RuntimeSnapshot } from "./runtime";

export interface OutputEntry {
  id: string;
  workspaceKey: string;
  fileUri: string;
  fileName: string;
  label: string;
  startLine: number;
  endLine: number;
  dirty: boolean;
  code: string;
  state: "queued" | "running" | "success" | "error" | "cancelled";
  response?: EvalResponse;
  failure?: string;
}
export interface WorkspaceView {
  key: string;
  name: string;
  snapshot: RuntimeSnapshot;
}
export function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
export function plainText(value: string): string {
  // Strip OSC and CSI terminal controls; never interpret output as HTML.
  return value
    .replace(/\x1b\][^\x07]*(?:\x07|\x1b\\)/g, "")
    .replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "");
}
function textBlock(
  title: string,
  text: string,
  truncated = false,
  kind = "",
): string {
  return `<section class="output ${kind}"><div class="section-label">${escapeHtml(title)}${
    truncated ? '<span class="truncate">truncated</span>' : ""
  }</div><pre>${escapeHtml(plainText(text))}</pre></section>`;
}
function entryHtml(entry: OutputEntry): string {
  const output = entry.response?.execution;
  const error = entry.response?.error;
  let content = "";
  if (output?.stdout) {
    content += textBlock("stdout", output.stdout, output.stdout_truncated);
  }
  if (output?.stderr) {
    content += textBlock(
      "stderr",
      output.stderr,
      output.stderr_truncated,
      "stderr",
    );
  }
  if (output?.result_repr !== null && output?.result_repr !== undefined) {
    content += textBlock(
      "Result",
      output.result_repr,
      output.result_repr_truncated,
      "result",
    );
  }
  if (error) {
    content += `<section class="output error-output"><strong>${escapeHtml(error.type)}: ${escapeHtml(plainText(error.message))}</strong>${
      error.traceback
        ? `<pre>${escapeHtml(plainText(error.traceback))}</pre>`
        : ""
    }</section>`;
  }
  if (entry.failure) {
    content += textBlock(
      entry.state === "cancelled" ? "Cancelled" : "Runtime error",
      entry.failure,
      false,
      "error-output",
    );
  }
  if (entry.response?.busy) {
    content +=
      '<p class="busy-note">Execution may still be running. Interrupt or restart before running more code.</p>';
  }
  if (!content) {
    content = `<p class="muted">${entry.state === "queued" ? "Preparing runtime or waiting for earlier executions…" : entry.state === "running" ? "Executing in the persistent Python session…" : "Completed with no output."}</p>`;
  }
  return `<article class="entry ${entry.state}"><div class="entry-heading"><div>
    <span class="badge ${entry.state}">${entry.state}</span><strong>${escapeHtml(entry.label)}</strong>
    <button class="source" data-action="source" data-id="${escapeHtml(entry.id)}">${escapeHtml(entry.fileName)} · lines ${entry.startLine + 1}–${Math.max(entry.startLine + 1, entry.endLine)}</button>
    ${entry.dirty ? '<span class="muted">unsaved buffer</span>' : ""}</div>
    <span class="duration">${output ? `${output.duration_ms} ms` : ""}</span></div>
    <details><summary>Executed code</summary><pre>${escapeHtml(entry.code)}</pre></details>${content}</article>`;
}

/** No user-controlled HTML/JS/URIs. All user text goes through escaping. */
export function renderOutputs(
  entries: OutputEntry[],
  workspaces: WorkspaceView[],
  selectedKey: string | undefined,
  nonce: string,
): string {
  const workspace =
    workspaces.find((item) => item.key === selectedKey) ?? workspaces[0];
  const state = workspace?.snapshot.state ?? "stopped";
  const busy = state === "busy" || state === "unresponsive";
  const visible = entries
    .filter((entry) => entry.workspaceKey === workspace?.key)
    .slice()
    .reverse();
  const options = workspaces
    .map(
      (item) =>
        `<option value="${escapeHtml(item.key)}" ${item.key === workspace?.key ? "selected" : ""}>${escapeHtml(item.name)}</option>`,
    )
    .join("");
  return `<!doctype html><html><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'nonce-${nonce}'; script-src 'nonce-${nonce}';">
    <title>Neura Outputs</title><style nonce="${nonce}">
    *{box-sizing:border-box}body{padding:0;margin:0;background:var(--vscode-editor-background);color:var(--vscode-foreground);font:var(--vscode-font-size) var(--vscode-font-family)}
    header{position:sticky;top:0;z-index:1;background:var(--vscode-editor-background);border-bottom:1px solid var(--vscode-panel-border);padding:18px 22px}
    .brand{display:flex;align-items:center;gap:10px;margin-bottom:14px}.mark{background:var(--vscode-button-background);color:var(--vscode-button-foreground);width:28px;height:28px;border-radius:7px;display:grid;place-items:center;font-size:18px;font-weight:700}
    h1{font-size:16px;font-weight:600;margin:0} .brand .muted{margin-left:auto} .toolbar{display:flex;flex-wrap:wrap;gap:7px;align-items:center}
    button,select{font:inherit;border:1px solid var(--vscode-button-border,transparent);border-radius:4px;padding:6px 10px;color:var(--vscode-button-secondaryForeground);background:var(--vscode-button-secondaryBackground);cursor:pointer}
    button:hover{background:var(--vscode-button-secondaryHoverBackground)}button:disabled{opacity:.45;cursor:default}select{background:var(--vscode-dropdown-background);color:var(--vscode-dropdown-foreground);max-width:200px}.toolbar .spacer{flex:1}
    .runtime{margin-top:10px;color:var(--vscode-descriptionForeground);font-size:11px;overflow-wrap:anywhere}.runtime strong{color:var(--vscode-foreground)}
    main{padding:18px 22px}.entry{border:1px solid var(--vscode-panel-border);border-radius:7px;margin-bottom:16px;overflow:hidden}.entry-heading{padding:12px 14px;display:flex;justify-content:space-between;align-items:flex-start;gap:12px;background:var(--vscode-sideBar-background)}
    .entry-heading strong{font-weight:600}.source{display:block;padding:0;background:none;border:0;margin-top:7px;color:var(--vscode-textLink-foreground);font-size:11px;text-align:left}.source:hover{background:none;text-decoration:underline}.duration{font-size:11px;white-space:nowrap;color:var(--vscode-descriptionForeground)}
    .badge{display:inline-block;text-transform:uppercase;letter-spacing:.07em;font-size:9px;padding:3px 6px;margin-right:8px;border-radius:3px;background:var(--vscode-badge-background);color:var(--vscode-badge-foreground)}
    .badge.success{color:var(--vscode-testing-iconPassed,#6bbf8a)}.badge.error{color:var(--vscode-errorForeground)}.badge.running{color:var(--vscode-progressBar-background)}
    details{border-top:1px solid var(--vscode-panel-border);padding:10px 14px}summary{color:var(--vscode-descriptionForeground);font-size:11px;cursor:pointer}pre{font:var(--vscode-editor-font-size) var(--vscode-editor-font-family);line-height:1.55;white-space:pre-wrap;overflow-wrap:anywhere;margin:8px 0 0}
    .output{border-top:1px solid var(--vscode-panel-border);padding:12px 14px}.section-label{font-size:10px;text-transform:uppercase;letter-spacing:.09em;color:var(--vscode-descriptionForeground)}.truncate{margin-left:8px;color:var(--vscode-editorWarning-foreground)}.stderr{color:var(--vscode-editorWarning-foreground)}.result{background:var(--vscode-textCodeBlock-background)}.error-output{color:var(--vscode-errorForeground)}
    .muted{color:var(--vscode-descriptionForeground);font-size:12px}.entry>p{padding:0 14px}.busy-note{color:var(--vscode-editorWarning-foreground)}.empty{padding:38px 16px;text-align:center;border:1px dashed var(--vscode-panel-border);border-radius:8px}.empty h2{font-size:16px;font-weight:500}.empty p{line-height:1.7;color:var(--vscode-descriptionForeground)}kbd{padding:2px 5px;border-radius:3px;background:var(--vscode-textCodeBlock-background);font:inherit}
    </style></head><body data-workspace="${escapeHtml(workspace?.key ?? "")}">
    <header><div class="brand"><span class="mark">N</span><h1>Neura Outputs</h1><span class="muted">Persistent Python · local execution</span></div>
    <div class="toolbar"><select id="workspace" aria-label="Workspace">${options}</select><button data-action="restart" ${!workspace || state === "stopping" ? "disabled" : ""}>Restart runtime</button><button data-action="interrupt" ${!busy ? "disabled" : ""}>Interrupt</button><span class="spacer"></span><button data-action="log">Runtime log</button><button data-action="clear">Clear outputs</button></div>
    <div class="runtime"><strong>${escapeHtml(state)}</strong>${workspace?.snapshot.pid ? ` · PID ${workspace.snapshot.pid}` : ""}${workspace ? ` · ${escapeHtml(workspace.snapshot.python)}` : ""}${workspace?.snapshot.pending ? ` · ${workspace.snapshot.pending} active/queued` : ""}</div></header>
    <main>${visible.length ? visible.map(entryHtml).join("") : '<section class="empty"><h2>Your workspace, alive.</h2><p>Open a Python file and run a selection or a <code># %%</code> cell.<br>Use <strong>Neura: Run Cell</strong> or <strong>Neura: Run Selection</strong> in the Command Palette.<br>Variables persist until you restart the runtime.</p></section>'}</main>
    <script nonce="${nonce}">const api=acquireVsCodeApi();document.querySelectorAll('[data-action]').forEach(button=>button.addEventListener('click',()=>api.postMessage({type:button.dataset.action,workspaceKey:document.body.dataset.workspace,id:button.dataset.id})));document.getElementById('workspace').addEventListener('change',event=>api.postMessage({type:'selectWorkspace',workspaceKey:event.target.value}));</script>
    </body></html>`;
}
