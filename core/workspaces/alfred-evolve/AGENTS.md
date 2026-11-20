# 🧬 Workspace `alfred-evolve` — Évolutivité Mono-Repo en Worktree Isolé & Triade TDD Sans Conflit d'Intérêts

> **Routage Modèle (Brain)** : `gemini-pro-orchestrator` (User-Initiated ou Délégation authentifiée SPF/DKIM)  
> **Racine Alfred & CLI** : `/root/Alfred` (`${ALFRED_ROOT_DIR:-/root/Alfred}/.venv/bin/alfred`)  
> **Pipeline Obligatoire** : **1. Analyse `Read-Only` $\rightarrow$ 2. Gate `implementation_plan.md` $\rightarrow$ 3. Triade TDD Isolée (`tdd-red` $\rightarrow$ `tdd-green` $\rightarrow$ `tdd-verifier`) en `git worktree` + PR Mono-Repo**

## 1. Étape 1 — Qualification Technique & Sécurité (`Read-Only`)
- Inspecte `core/` et `instance/extensions/` pour séparer strictement la logique générique open-source (`core/alfred/`, exemples dans `instance/examples/`) de la configuration privée gitignorée (`instance/config/settings.yaml`, `instance/extensions/`).
- Si la demande provient d'un e-mail, vérifie impérativement qu'elle émane d'une adresse propriétaire authentifiée SPF/DKIM (`EmailSecurityService`).

## 2. Étape 2 — Gate Systématique (`implementation_plan.md`)
- `plan-synthesizer` rédige **`implementation_plan.md`** (`UserFacing: true`, `RequestFeedback: true`) détaillant l'architecture OOP (4 couches `domain <- repositories <- services <- controllers <- main`), l'analyse des **régressions** potentielles / **non-régression**, les 3 niveaux de contrats de tests (**unitaires**, **fonctionnels**, **E2E / bout en bout**) et la campagne d'**adversarial testing `swarm`**.
- Aucune branche ni modification de code n'est créée avant validation **`Proceed`**.

## 3. Étape 3 — Exécution TDD par Triade Isolée en `git worktree` (`EvolveWorktreeService`)
Contrairement aux workspaces opérationnels servis par `plan-executor`, tout développement de code dans `alfred-evolve` s'exécute dans un `git worktree` isolé (`worktrees/<proposal_id>`, branche `evolve/<proposal_id>`) via **3 sous-agents à contextes séparés** :
1. **`tdd-red`** *(Tests seuls)* : écrit les tests initialement en échec sous `core/tests/` aux 3 niveaux (**unitaires**, **fonctionnels** et **E2E**), incluant **non-régression** et scénarios **adversarial `swarm`** (interdiction absolue de modifier `core/alfred/`).
2. **`tdd-green`** *(Production seule)* : implémente le code de production respectant `<= 500L/fichier`, `<= 60L/méthode` hors docstring et 100 % docstrings Google (interdiction absolue de modifier, affaiblir ou ignorer un fichier sous `core/tests/`).
3. **`tdd-verifier`** *(Audit Read-Only indépendant)* : vérifie l'intégrité des tests (unitaires, fonctionnels, E2E, non-régression, adversarial `swarm`) et valide les **5 gardiens déterministes bloquants** :
   - `check_code_architecture.py`
   - `check-upstream-leak.sh` (0 donnée personnelle dans le mono-repo `alfred-core`)
   - `ruff check` & `mypy --strict`
   - `pytest --cov=core --cov-fail-under=90`
4. Ouverture automatique de la Pull Request Mono-Repo sur **`alfred-core`** (`alfred evolve --id "<slug>" --title "<titre>" --description "<spec>" --execute`).
