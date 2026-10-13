import { EvalResponse, RichOutput } from "./protocol";
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
/**
 * Allowlist sanitizer for captured HTML.
 *
 * The runtime returns raw HTML by design (it executes with the user's own
 * permissions, so it is not a security boundary), which makes this the
 * security boundary for the webview. Anything not on the allowlist is dropped,
 * including event handlers and non-http(s)/data:image URLs.
 */
const ALLOWED_TAGS = new Set([
  "a", "b", "blockquote", "br", "caption", "code", "col", "colgroup", "dd",
  "div", "dl", "dt", "em", "figcaption", "figure", "h1", "h2", "h3", "h4",
  "h5", "h6", "hr", "i", "img", "li", "ol", "p", "pre", "small", "span",
  "strong", "sub", "summary", "sup", "table", "tbody", "td", "tfoot", "th",
  "thead", "tr", "u", "ul", "s",
]);
const ALLOWED_ATTRS = new Set([
  "alt", "class", "colspan", "height", "href", "rowspan", "src", "title",
  "width",
]);
const ALLOWED_STYLE = new Set([
  "color", "background-color", "border", "border-collapse", "font-size",
  "font-style", "font-weight", "font-family", "margin", "margin-left",
  "margin-right", "padding", "padding-left", "padding-right", "text-align",
  "text-decoration", "vertical-align", "white-space", "width", "max-width",
]);
const SAFE_URL = /^(?:https?:|mailto:)/i;
const SAFE_IMAGE = /^data:image\/(?:png|jpeg|jpg|gif|webp);base64,[a-z0-9+/=\s]+$/i;

export function sanitizeHtml(html: string): string {
  const tagPattern = /<\/?[a-zA-Z][^>]*>/g;
  const attrPattern =
    /([a-zA-Z_:][\w:.-]*)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+)))?/g;
  const voidTags = new Set(["br", "hr", "img", "col"]);
  const dropWithContent = new Set([
    "script", "style", "iframe", "object", "embed", "link", "meta",
    "template", "noscript", "title", "base", "form", "input", "button",
  ]);
  let result = "";
  let cursor = 0;
  let match: RegExpExecArray | null;
  while ((match = tagPattern.exec(html))) {
    result += escapeHtml(html.slice(cursor, match.index));
    cursor = match.index + match[0].length;
    const tag = match[0];
    const name = (tag.match(/^<\/?\s*([a-zA-Z][\w-]*)/) ?? [])[1]?.toLowerCase();
    if (!name) {
      continue;
    }
    if (!ALLOWED_TAGS.has(name)) {
      if (dropWithContent.has(name) && !tag.startsWith("</")) {
        // Skip everything up to the matching close tag (or the next tag).
        const closing = new RegExp(`</\s*${name}\s*>`, "i");
        const rest = html.slice(cursor);
        const found = closing.exec(rest);
        cursor += found ? found.index + found[0].length : 0;
      }
      continue;
    }
    if (tag.startsWith("</")) {
      if (!voidTags.has(name)) {
        result += `</${name}>`;
      }
      continue;
    }
    const body = tag.replace(/^<\s*[a-zA-Z][\w-]*/, "").replace(/\/?>$/, "");
    let attrs = "";
    let attribute: RegExpExecArray | null;
    attrPattern.lastIndex = 0;
    while ((attribute = attrPattern.exec(body))) {
      const attributeName = attribute[1].toLowerCase();
      if (
        !ALLOWED_ATTRS.has(attributeName) ||
        attributeName.startsWith("on") ||
        attributeName === "style"
      ) {
        if (attributeName === "style") {
          const safe = sanitizeStyle(attribute[2] ?? attribute[3] ?? attribute[4] ?? "");
          if (safe) {
            attrs += ` style="${escapeHtml(safe)}"`;
          }
        }
        continue;
      }
      const value = attribute[2] ?? attribute[3] ?? attribute[4] ?? "";
      if (attributeName === "href" && !SAFE_URL.test(value.trim())) {
        continue;
      }
      if (attributeName === "src" && !(name === "img" && SAFE_IMAGE.test(value.trim()))) {
        continue;
      }
      attrs += ` ${attributeName}="${escapeHtml(value)}"`;
    }
    result += voidTags.has(name) ? `<${name}${attrs}>` : `<${name}${attrs}>`;
    if (voidTags.has(name)) {
      continue;
    }
  }
  result += escapeHtml(html.slice(cursor));
  return result;
}
function sanitizeStyle(value: string): string {
  return value
    .split(";")
    .map((rule) => rule.trim())
    .filter((rule) => {
      const property = rule.split(":")[0]?.trim().toLowerCase();
      return !!property && ALLOWED_STYLE.has(property) && !/url\(/i.test(rule);
    })
    .join("; ");
}
function formatCell(value: unknown): string {
  if (value === null || value === undefined) {
    return '<span class="null">null</span>';
  }
  return escapeHtml(plainText(String(value)));
}
function dataframeTable(output: Extract<RichOutput, { kind: "dataframe" }>): string {
  const head = output.columns
    .map(
      (column) =>
        `<th>${escapeHtml(column.name)}<span class="dtype">${escapeHtml(column.dtype)}</span></th>`,
    )
    .join("");
  const rows = output.rows
    .map(
      (row) =>
        `<tr>${row.map((cell) => `<td>${formatCell(cell)}</td>`).join("")}</tr>`,
    )
    .join("");
  return `<section class="output rich"><div class="section-label">DataFrame ${output.shape[0]} × ${output.shape[1]}${
    output.truncated ? '<span class="truncate">truncated</span>' : ""
  }</div><div class="table-scroll"><table><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table></div></section>`;
}
function imageBlock(output: Extract<RichOutput, { kind: "image_png" }>): string {
  return `<section class="output rich"><div class="section-label">Plot${
    output.truncated ? '<span class="truncate">truncated</span>' : ""
  }</div><img class="plot" alt="Matplotlib figure" src="data:image/${escapeHtml(
    output.format,
  )};base64,${output.data}" ${
    output.width ? `width="${output.width}"` : ""
  }></section>`;
}
function htmlBlock(output: Extract<RichOutput, { kind: "html" }>): string {
  return `<section class="output rich"><div class="section-label">HTML output${
    output.truncated ? '<span class="truncate">truncated</span>' : ""
  }</div><div class="sanitized-html">${sanitizeHtml(output.html)}</div></section>`;
}
function richBlock(output: RichOutput): string {
  if (output.kind === "html") {
    return htmlBlock(output);
  }
  if (output.kind === "image_png") {
    return imageBlock(output);
  }
  if (output.kind === "dataframe") {
    return dataframeTable(output);
  }
  return textBlock("Note", output.text, output.truncated === true);
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
  for (const item of output?.outputs ?? []) {
    content += richBlock(item);
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
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'nonce-${nonce}'; script-src 'nonce-${nonce}'; img-src data:;">
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
    .muted{color:var(--vscode-descriptionForeground);font-size:12px}
    .rich .section-label{display:flex;align-items:center;gap:8px}
    img.plot{max-width:100%;height:auto;background:var(--vscode-textCodeBlock-background);border-radius:5px;margin-top:8px}
    .table-scroll{overflow:auto;max-height:340px;margin-top:8px;border:1px solid var(--vscode-panel-border);border-radius:5px}
    .sanitized-html table,.table-scroll table{border-collapse:collapse;font-size:12px;width:100%}
    .sanitized-html th,.sanitized-html td,.table-scroll th,.table-scroll td{border:1px solid var(--vscode-panel-border);padding:5px 9px;text-align:left;white-space:nowrap}
    .table-scroll th{position:sticky;top:0;background:var(--vscode-sideBar-background);z-index:1}
    .table-scroll .dtype{display:block;font-weight:400;font-size:10px;color:var(--vscode-descriptionForeground)}
    .table-scroll .null{color:var(--vscode-descriptionForeground);font-style:italic}
    .sanitized-html{margin-top:8px;overflow:auto;max-height:340px;font-size:12px;line-height:1.5}
    .sanitized-html a{color:var(--vscode-textLink-foreground)}
    .sanitized-html pre{white-space:pre-wrap}.entry>p{padding:0 14px}.busy-note{color:var(--vscode-editorWarning-foreground)}.empty{padding:38px 16px;text-align:center;border:1px dashed var(--vscode-panel-border);border-radius:8px}.empty h2{font-size:16px;font-weight:500}.empty p{line-height:1.7;color:var(--vscode-descriptionForeground)}kbd{padding:2px 5px;border-radius:3px;background:var(--vscode-textCodeBlock-background);font:inherit}
    </style></head><body data-workspace="${escapeHtml(workspace?.key ?? "")}">
    <header><div class="brand"><span class="mark">N</span><h1>Neura Outputs</h1><span class="muted">Persistent Python · local execution</span></div>
    <div class="toolbar"><select id="workspace" aria-label="Workspace">${options}</select><button data-action="restart" ${!workspace || state === "stopping" ? "disabled" : ""}>Restart runtime</button><button data-action="interrupt" ${!busy ? "disabled" : ""}>Interrupt</button><span class="spacer"></span><button data-action="log">Runtime log</button><button data-action="clear">Clear outputs</button></div>
    <div class="runtime"><strong>${escapeHtml(state)}</strong>${
      workspace?.snapshot.backend === "bridge"
        ? ` · attached to ${escapeHtml(workspace.snapshot.kernelName ?? "notebook kernel")}`
        : ""
    }${workspace?.snapshot.pid ? ` · PID ${workspace.snapshot.pid}` : ""}${workspace ? ` · ${escapeHtml(workspace.snapshot.python)}` : ""}${workspace?.snapshot.pending ? ` · ${workspace.snapshot.pending} active/queued` : ""}</div></header>
    <main>${visible.length ? visible.map(entryHtml).join("") : '<section class="empty"><h2>Your workspace, alive.</h2><p>Open a Python file and run a selection or a <code># %%</code> cell.<br>Use <strong>Neura: Run Cell</strong> or <strong>Neura: Run Selection</strong> in the Command Palette.<br>Variables persist until you restart the runtime.</p></section>'}</main>
    <script nonce="${nonce}">const api=acquireVsCodeApi();document.querySelectorAll('[data-action]').forEach(button=>button.addEventListener('click',()=>api.postMessage({type:button.dataset.action,workspaceKey:document.body.dataset.workspace,id:button.dataset.id})));document.getElementById('workspace').addEventListener('change',event=>api.postMessage({type:'selectWorkspace',workspaceKey:event.target.value}));</script>
    </body></html>`;
}
