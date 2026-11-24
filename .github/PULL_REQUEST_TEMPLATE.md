## `alfred-core` Evolution Summary

- **Proposal ID**: `evolve/<proposal_id>`
- **Mode**: `upstream-pr` (`--hybrid --core-complete` if applicable)

## V8.0 Mono-Repo & Governance Checklist

- [ ] **Generic Scope**: zero private logic or personal data (`instance/config/`, `instance/data/`, `instance/secrets/`, and active `instance/extensions/` excluded; anonymized examples under `instance/examples/`).
- [ ] **`core/` Standalone Completeness**: if hybrid, the `core/` capability works 100% standalone without `instance/` (`check_code_architecture.py`: zero `Instance Coupling`).
- [ ] **Documentation & Living Spec (`doc-maintainer`)**: `README.md` (`<= 195` lines), agent/workspace manifests, templates, and `docs/SPEC.md` updated and synced (`alfred sync-spec --check --repo-root .`).
- [ ] **English-Only Repo & Bilingual FR/EN Runtime Preserved**: `check_english_only.py --repo-root .` passes with zero French prose in repo code/docs/templates while bilingual French and English runtime support remains intact.
- [ ] **All 6 Deterministic Guardians Green**: `check_code_architecture.py`, `check-upstream-leak.sh`, `check_english_only.py`, `ruff check`, `mypy --strict`, `pytest --cov=core --cov-fail-under=90`.
- [ ] **Breaking Changes**: if breaking (`[BREAKING]`), impacts are documented and flagged for maintainer review.
