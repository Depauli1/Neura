import { strict as assert } from "node:assert";
import { test } from "node:test";
import { cellAt } from "../cells";

const script = "import sys\n# %% First\na = 1\n#%% Second\na + 1\n";
test("preamble is an implicit cell and markers belong to the following cell", () => {
  assert.equal(cellAt(script, 0).code, "import sys");
  assert.equal(cellAt(script, 1).code, "a = 1");
  assert.equal(cellAt(script, 2).title, "First");
  assert.equal(cellAt(script, 3).code, "a + 1\n");
  assert.equal(cellAt(script, 3).startLine, 4);
  assert.equal(cellAt(script, 5).endLine, 6);
});
test("no markers executes the entire file; CRLF and empty cells are handled", () => {
  assert.equal(cellAt("a=1\r\na+1\r\n", 1).code, "a=1\na+1\n");
  assert.equal(cellAt("# %%\n# %% next", 0).code, "");
  assert.equal(cellAt("", 0).code, "");
  assert.equal(cellAt(script, 999).code, "a + 1\n");
  assert.equal(cellAt(script, -1).code, "import sys");
});
test("markdown cells are identified without evaluating their body", () => {
  const cell = cellAt(
    "# %% [markdown] Notes\n# hello\n# %% [code] Work\nx=1",
    1,
  );
  assert.equal(cell.kind, "markdown");
  assert.equal(cell.title, "Notes");
  assert.equal(cellAt("# %% [code] Work\nx=1", 0).kind, "code");
});
test("marker text inside quoted strings/comments is not a cell boundary", () => {
  const code = `text = """a\n# %% not a marker\nb"""\nprint('# %% fake')\n# comment with '''\n# %% Real\n42`;
  assert.equal(cellAt(code, 2).endLine, 5);
  assert.equal(cellAt(code, 5).title, "Real");
  assert.equal(cellAt(code, 6).code, "42");
  const single = "text = 'continued\\\n# %% inside string'\n# %% next\n1";
  assert.equal(cellAt(single, 1).endLine, 2);
  assert.equal(cellAt(single, 2).code, "1");
});
