# 📅 Workspace `alfred-calendar` — Assistante Maternelle & Calendrier `Garde Nounou`

> **Mode d'Ouverture** : **Agent-Spawned Only** (routines planifiées & événements agenda)  
> **Racine Alfred & CLI** : `/root/Alfred` (`${ALFRED_ROOT_DIR:-/root/Alfred}/.venv/bin/alfred`)  
> **Pipeline Obligatoire** : **Pattern Universel en 3 Étapes (Recherche Adaptative $\rightarrow$ Gate `implementation_plan.md` $\rightarrow$ Exécuteur Unique)**

## 1. Les 4 Workflows Déterministes `Garde Nounou`
1. **Initialisation sur Contrat Signé (`init-nounou`)** :
   - **Étape 1** : Lecture du contrat signé sur Google Drive (`research-drive`).
   - **Étape 2** : Soumission des clauses extraites dans `implementation_plan.md` (`RequestFeedback: true`).
   - **Étape 3** : Création du calendrier Google dédié **`Garde Nounou`**, sauvegarde de `nounou_contract.json` et de `02_assistante_maternelle.md` (avec Frontmatter YAML et backup `_ALFRED_REFERENTIEL_`).
2. **Routine du 20 du Mois $M$ pour $M+1$ (`seed-nounou`)** :
   - Calcul déterministe des créneaux de $M+1$ aux horaires exacts du contrat (excluant les 11 jours fériés légaux français et les vacances enregistrées) $\rightarrow$ Gate `implementation_plan.md` $\rightarrow$ insertion dans `Garde Nounou`.
3. **Vacances & Préavis Contractuel (~1 mois / `check-vacation`)** :
   - Vérification automatique du délai de préavis contractuel (~30 jours avant le début des congés) et présentation de l'alerte ou validation dans `implementation_plan.md`.
4. **Clôture Mensuelle Pajemploi à `last_day - 3` (`close-pajemploi`, février 28/29j inclus)** :
   - Calcul 100 % déterministe à 0 token (`PajemploiCalculator` : heures normales, heures majorées, déduction Cour de Cassation, indemnités entretien et repas) soumis dans `implementation_plan.md`.
