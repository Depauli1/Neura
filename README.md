# Neura

An AI-native environment for data science, built as a VS Code extension.

- **Data-aware:** profiles real pandas DataFrames
- **Execution-grounded:** runs code, reads results/errors, self-corrects
- **Runtime-aware:** works with `.ipynb` (local kernel attach) and `.py` (`# %%` cells via a managed runtime)
- **Trust-oriented:** guardrails, reviewable diffs, local-first

Status: pre-alpha (V1 in design). See [`docs/`](docs/).

## Docs
- [Whitepaper](docs/whitepaper.md)
- [V1 decisions](docs/v1-decisions.md)
- [Runtime API](docs/api/runtime-api.md)
- [Runtime setup, implemented endpoints, and limitations](docs/runtime-development.md)

## Layout
- `extension/` VS Code extension (TypeScript)
- `python/` `neura_core`, `neura_runtime`, `neura_bridge`
- `fixtures/` test datasets and scenarios

## Develop the managed runtime

Using Python 3.10+ in a virtual environment:

```sh
python -m pip install -e "python[dev]"
pytest python/tests
ruff check python
ruff format --check python
```

Launch with a fresh random token and an existing workspace directory:

```sh
TOKEN=$(python -c 'import secrets; print(secrets.token_urlsafe(32))')
python -m neura_runtime --port 0 --token="$TOKEN" --workspace "$PWD"
```

The runtime prints a readiness JSON line with its port and PID, binds only to
`127.0.0.1`, and requires Bearer authentication. Implemented: `GET /health`,
`POST /eval` (persistent IPython state, text/error capture, busy conflicts, timeouts),
and `POST /interrupt`. See the [runtime guide](docs/runtime-development.md) for
request/response examples and budget/interrupt limitations.

## Run Python from VS Code

The first managed `.py` execution slice is implemented: one persistent runtime
per workspace folder, Run Cell (`# %%`) / Run Selection, a text Outputs panel,
and restart/interrupt controls. Uses the Python extension's selected interpreter
or the `neura.pythonPath` override; the runtime must be installed there.

1. Install the runtime in your selected Python environment as above.
2. Run `cd extension && npm ci && npm run compile`.
3. Open the repository root in desktop VS Code and press **F5 → Neura Extension**.
4. In the Development Host, select that interpreter (or set `neura.pythonPath`),
   open `fixtures/runtime-demo.py`, and use **Neura: Run Cell** / **Run Selection**.

See [VS Code setup, commands, settings, and smoke test](docs/extension-development.md).
Extension automated checks: `npm test` in `extension/` (requires the Python runtime;
set `NEURA_TEST_PYTHON` for a non-PATH interpreter).

The notebook bridge, DataFrame/plot outputs, profiling, and AI chat remain future
work. This is a usable execution foundation, **not complete V1**.
