# Personal Extensions (`instance/extensions/`)

This directory hosts personal automations tailored for your instance by **`alfred-evolve`**.

## Mandatory Rules
1. **Never version active extensions in Git**: every `instance/extensions/*.py` file (outside `__init__.py` and `*.example.py`) stays strictly gitignored and is automatically backed up to your Google Drive knowledge folder (`_ALFRED_REFERENTIEL_`).
2. **Single `AlfredExtension` contract** (`core/alfred/domain/extension_protocol.py`):
   - Expose a `build_extension() -> AlfredExtension` factory (or a class implementing `extension_id`, `description`, `validate_config`, `run_periodic`).
   - Comply with all code guardians (`<= 500L/file`, `<= 60L/method`, English Google docstrings with `Args:`, `ruff`, `mypy --strict`).
3. **Ready-to-copy complete examples**: see [`../examples/`](../examples/).
