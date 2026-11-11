# 📅 Workspace `alfred-calendar` — Agenda Principal (`[HOLD]` & Invitations) & `Garde Nounou`

> **Mode d'Ouverture** : **Agent-Spawned Only** (routines planifiées, `[HOLD]` sur brouillons & validations d'événements)  
> **Racine Alfred & CLI** : `/root/Alfred` (`${ALFRED_ROOT_DIR:-/root/Alfred}/.venv/bin/alfred`)  
> **Pipeline Obligatoire** : **Pattern Universel en 3 Étapes (Recherche Adaptative $\rightarrow$ Gate `implementation_plan.md` $\rightarrow$ Exécuteur Unique)**

## 1. Agenda Principal (`primary`) — Cycle `[HOLD]` Allowlisté $\rightarrow$ Gate d'Invitation Obligatoire
1. **Pose de `[HOLD]` lors d'une proposition de créneau (`create-event --hold`)** :
   - Bloque l'agenda principal en `opaque` (`[HOLD] <Objet>`, corrélé via `alfred_thread_id`).
   - **Allowlist stricte des invités sur `[HOLD]` (`alfred hold-allowlist --list`)** : seules les adresses explicitement allowlistées (adresses propriétaire authentifiées + contacts proches configurés dans `calendar_hold_allowlist.json`) reçoivent immédiatement le `[HOLD]` (`sendUpdates="all"`). Tout interlocuteur externe non allowlisté est différé dans `alfred_candidate_attendees` (**zéro invitation externe envoyée au stade `[HOLD]`**).
2. **Conversion de `[HOLD]` & Envoi d'Invitation sous Gate Obligatoire (`confirm-event`)** :
   - Lorsqu'un créneau/meeting est validé par l'interlocuteur, **aucune invitation Google Calendar n'est envoyée sans Gate bloquante** (`implementation_plan.md` avec `RequestFeedback: true`).
   - Après clic utilisateur sur **`Proceed`**, `alfred confirm-event --send-invites` convertit le `[HOLD]` retenu en événement ferme (retrait du préfixe `[HOLD]`), invite toutes les personnes concernées (`attendees` + `sendUpdates="all"`), et libère automatiquement les éventuels autres `[HOLD]` alternatifs du même fil Gmail.

## 2. Les 4 Workflows Déterministes `Garde Nounou`
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
