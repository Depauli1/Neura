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

## Layout
- `extension/` VS Code extension (TypeScript)
- `python/` `neura_core`, `neura_runtime`, `neura_bridge`
- `fixtures/` test datasets and scenarios
