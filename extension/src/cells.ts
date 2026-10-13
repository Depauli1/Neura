/** # %% parsing independent of VS Code, so cursor/boundary behavior is testable. */
export interface PythonCell {
  code: string;
  startLine: number; // zero-based body start
  endLine: number; // exclusive
  markerLine?: number;
  kind: "code" | "markdown";
  title?: string;
}

/** Ignore marker-looking text inside strings, including triple-quoted strings. */
function markerLines(lines: string[]): Map<number, string> {
  const markers = new Map<number, string>();
  let quote = "";
  let triple = false;
  for (let lineNumber = 0; lineNumber < lines.length; lineNumber++) {
    const line = lines[lineNumber];
    const marker = !quote && /^\s*#\s*%%(.*)$/.exec(line);
    if (marker) {
      markers.set(lineNumber, marker[1].trim());
      continue;
    }
    let continued = false;
    for (let i = 0; i < line.length; i++) {
      const char = line[i];
      if (char === "\\") {
        if (i === line.length - 1) {
          continued = true;
        }
        i++;
        continue;
      }
      if (quote) {
        if (triple && line.slice(i, i + 3) === quote.repeat(3)) {
          quote = "";
          triple = false;
          i += 2;
        } else if (!triple && char === quote) {
          quote = "";
        }
      } else if (char === "#") {
        break;
      } else if (char === "'" || char === '"') {
        quote = char;
        triple = line.slice(i, i + 3) === char.repeat(3);
        if (triple) {
          i += 2;
        }
      }
    }
    // An invalid unterminated single-line string must not hide later cells.
    if (quote && !triple && !continued) {
      quote = "";
    }
  }
  return markers;
}

export function cellAt(text: string, cursorLine: number): PythonCell {
  const lines = text.split(/\r\n|\n|\r/);
  const markers = markerLines(lines);
  const cursor = Math.max(0, Math.min(lines.length - 1, cursorLine));
  let markerLine: number | undefined;
  let endLine = lines.length;
  for (const line of markers.keys()) {
    if (line <= cursor) {
      markerLine = line;
    } else {
      endLine = line;
      break;
    }
  }
  const startLine = markerLine === undefined ? 0 : markerLine + 1;
  const title = markerLine === undefined ? undefined : markers.get(markerLine);
  return {
    code: lines.slice(startLine, endLine).join("\n"),
    startLine,
    endLine,
    markerLine,
    kind: title && /^\[markdown\](?:\s|$)/i.test(title) ? "markdown" : "code",
    title: title?.replace(/^\[(?:code|markdown)\]\s*/i, "") || undefined,
  };
}
