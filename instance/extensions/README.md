# Extensions Personnelles (`instance/extensions/`)

Ce dossier accueille les automatisations personnelles créées sur mesure par **`alfred-evolve`**.

## Règles Obligatoires
1. **Zéro versionnement Git des extensions actives** : tout fichier `instance/extensions/*.py` (hors `__init__.py` et `*.example.py`) reste strictement ignoré par Git et sauvegardé automatiquement dans votre dossier référentiel Google Drive.
2. **Contrat unique `AlfredExtension`** (`core/alfred/domain/extension_protocol.py`) :
   - Expose une fabrique `build_extension() -> AlfredExtension` (ou une classe implémentant `extension_id`, `description`, `validate_config`, `run_periodic`).
   - Respecte les 5 gardiens déterministes (`<= 500L/fichier`, `<= 60L/méthode`, docstrings Google `Args:`, `ruff`, `mypy --strict`).
3. **Exemples complets prêts à copier** : voir [`../examples/`](../examples/).
