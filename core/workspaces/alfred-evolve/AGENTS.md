# 🧬 Workspace `alfred-evolve` — Évolutivité TDD en Worktree Isolé & PR Dual-Git

> **Mode d'Ouverture** : **User-Initiated ou Signal Capté**  
> **Pipeline Obligatoire** : **1. Analyse & Signaux Turbo (`Read-Only`) $\rightarrow$ 2. Gate `implementation_plan.md` $\rightarrow$ 3. Exécution TDD en `git worktree` + Pull Requests sur les 2 Dépôts Git**

## 1. Étape 1 — Compréhension du Besoin & Captation de Signaux (`Read-Only`)
- Mobilise de façon adaptative 0, 1 ou plusieurs sous-agents (`research-drive`, `research-gmail-cal`, `research-web`) ainsi que l'inspection du code `core/` et `instance/extensions/` pour Qualifier l'évolution (`alfred-core` générique vs `alfred-instance` personnel).

## 2. Étape 2 — Gate Systématique (`implementation_plan.md`)
- `plan-synthesizer` rédige **`implementation_plan.md`** (`UserFacing: true`, `RequestFeedback: true`) détaillant l'architecture OOP, le plan de tests TDD (`Red -> Green -> Refactor`) et la répartition entre les 2 dépôts Git.
- Aucune branche ni modification de code n'est appliquée avant `Proceed`.

## 3. Étape 3 — Exécution TDD Isolée & Génération Automatique des 2 Pull Requests (`EvolveWorktreeService`)
Une fois le plan validé :
1. Création de `git worktree` isolés sur la branche `evolve/<proposal_id>` pour `alfred-core` (`/root/Alfred/.git`) et `alfred-instance` (`/root/Alfred/instance/.git`).
2. Implémentation en TDD strict (`<= 500L/fichier`, `<= 60L/méthode` hors docstring, 100 % docstrings Google).
3. Passage bloquant des 5 gardiens déterministes :
   - `check_code_architecture.py`
   - `check-upstream-leak.sh` (garantissant 0 donnée personnelle dans `alfred-core`)
   - `ruff check` & `mypy --strict`
   - `pytest --cov=core --cov-fail-under=90`
4. Génération automatique des **Pull Requests (PR) sur les 2 dépôts Git** :
   - PR #1 sur le dépôt générique **`alfred-core`** (`/root/Alfred/.git`),
   - PR #2 sur le dépôt personnel **`alfred-instance`** (`/root/Alfred/instance/.git`, avec `secrets/` et `data/` strictement ignorés).
