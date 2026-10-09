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

The notebook bridge, DataFrame/plot outputs, and VS Code command wiring are still
scaffolds or future work.
