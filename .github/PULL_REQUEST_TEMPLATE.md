## Résumé de l'évolution `alfred-core`

- **Proposal ID** : `evolve/<proposal_id>`
- **Mode** : `upstream-pr` (`--hybrid --core-complete` si applicable)

## Checklist Mono-Repo & Gouvernance V8.0

- [ ] **Périmètre générique** : aucune logique privée ni donnée personnelle (`instance/config/`, `instance/data/`, `instance/secrets/`, `instance/extensions/` actifs exclus ; exemples anonymisés dans `instance/examples/`).
- [ ] **Autonomie `core/`** : si évolution hybride, la fonctionnalité `core/` est 100 % autonome sans dépendance sur `instance/` (`check_code_architecture.py` : zéro `Instance Coupling`).
- [ ] **Living Spec à jour** : `docs/SPEC.md` synchronisé (`alfred sync-spec --check --repo-root .`).
- [ ] **5 Gardiens Déterministes verts** : `check_code_architecture.py`, `check-upstream-leak.sh`, `ruff check`, `mypy --strict`, `pytest --cov=core --cov-fail-under=90`.
- [ ] **Breaking Changes** : si rupture de compatibilité (`[BREAKING]`), impacts documentés et soumis à arbitrage mainteneur.
