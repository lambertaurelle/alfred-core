# 🧬 Workspace `alfred-evolve` — Évolutivité Mono-Repo en Worktree Isolé & Triade TDD Sans Conflit d'Intérêts

> **Routage Modèle (Brain)** : `gemini-pro-orchestrator` (User-Initiated ou Délégation authentifiée SPF/DKIM)  
> **Racine Alfred & CLI** : `/root/Alfred` (`${ALFRED_ROOT_DIR:-/root/Alfred}/.venv/bin/alfred`)  
> **Pipeline Obligatoire** : **1. Qualification `evolve-qualifier` (`Read-Only`) $\rightarrow$ 2. Gate `implementation_plan.md` $\rightarrow$ 3. Triade TDD Isolée (`tdd-red` $\rightarrow$ `tdd-green` $\rightarrow$ `tdd-verifier`) selon `--mode` (`instance` | `upstream-pr` | `local-fork`)**

## 1. Étape 1 — Qualification Architecturale & Sécurité (`evolve-qualifier`, `Read-Only`)
- **`evolve-qualifier`** inspecte `docs/SPEC.md`, `core/` et `instance/` pour trancher entre les 3 modes :
  - **`--mode instance`** : logique purement personnelle sous `instance/` uniquement (0 PR, `docs/SPEC.md` inchangé ; blocage déterministe si modification hors `instance/`).
  - **`--mode upstream-pr`** : évolution générique dans `core/` (ou exemples anonymes dans `instance/examples/`). Si `--hybrid`, la brique `core/` doit être **100 % autonome** (`--core-complete`) sans dépendance `instance/`. Synchronisation automatique de `docs/SPEC.md` (`sync-spec`), détection `[BREAKING]` (`--breaking`) et fallback fork GitHub si `origin` refuse le push.
  - **`--mode local-fork`** : modification locale de `core/` committée sur `evolve/<id>` sans ouvrir de PR amont.
- Si la demande provient d'un e-mail, vérifier impérativement qu'elle émane d'une adresse propriétaire authentifiée SPF/DKIM (`EmailSecurityService`).

## 2. Étape 2 — Gate Systématique (`implementation_plan.md`)
- `plan-synthesizer` rédige **`implementation_plan.md`** (`UserFacing: true`, `RequestFeedback: true`) détaillant le mode retenu (`instance` / `upstream-pr` / `local-fork`), l'architecture OOP (4 couches `domain <- repositories <- services <- controllers <- main`), l'analyse des **régressions** potentielles / **non-régression**, les 3 niveaux de contrats de tests (**unitaires**, **fonctionnels**, **E2E / bout en bout**) et la campagne d'**adversarial testing `swarm`**.
- Aucune branche ni modification de code n'est créée avant validation **`Proceed`**.

## 3. Étape 3 — Exécution TDD par Triade Isolée en `git worktree` (`EvolveWorktreeService`)
Contrairement aux workspaces opérationnels servis par `plan-executor`, tout développement `core/` dans `alfred-evolve` s'exécute dans un `git worktree` isolé (`worktrees/<proposal_id>`, branche `evolve/<proposal_id>`) via **3 sous-agents à contextes séparés** :
1. **`tdd-red`** *(Tests seuls)* : écrit les tests initialement en échec sous `core/tests/` aux 3 niveaux (**unitaires**, **fonctionnels** et **E2E**), incluant **non-régression** et scénarios **adversarial `swarm`** (interdiction absolue de modifier `core/alfred/`).
2. **`tdd-green`** *(Production seule)* : implémente le code de production respectant `<= 500L/fichier`, `<= 60L/méthode` hors docstring et 100 % docstrings Google (interdiction absolue de modifier, affaiblir ou ignorer un fichier sous `core/tests/`).
3. **`tdd-verifier`** *(Audit Read-Only indépendant)* : vérifie l'intégrité des tests (unitaires, fonctionnels, E2E, non-régression, adversarial `swarm`) et valide les **5 gardiens déterministes bloquants** + `sync-spec --check` :
   - `check_code_architecture.py --repo-root <worktree>` (incluant zéro `Instance Coupling`)
   - `check-upstream-leak.sh <worktree>` (0 donnée personnelle sur `HEAD`)
   - `ruff check core` & `mypy --strict core/alfred core/scripts core/tests`
   - `pytest --cov=core --cov-fail-under=90` & `alfred sync-spec --check --repo-root <worktree>`
4. Finalisation : `alfred evolve --id "<slug>" --title "<titre>" --description "<spec>" --mode <mode> --execute` (avec `--hybrid --core-complete` ou `--breaking` si applicable).
