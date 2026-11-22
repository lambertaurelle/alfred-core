# 📅 Workspace `alfred-calendar` — Agendas Multiples, Cycle `[HOLD]` & Exclusion Stricte d'Auto-Invitation

> **Routage Modèle (Muscle)** : `gemini-4-hydrogen-medium` (Agent-Spawned ou Délégation `alfred delegate --to calendar`)  
> **Racine Alfred & CLI** : `/root/Alfred` (`${ALFRED_ROOT_DIR:-/root/Alfred}/.venv/bin/alfred`)  
> **Pipeline Obligatoire** : **1. Inspection `Read-Only` $\rightarrow$ 2. Gate `implementation_plan.md` $\rightarrow$ 3. Exécuteur Léger `Hydrogen` (`plan-executor`)**

## 1. Étape 1 — Lecture Multi-Calendriers & Disponibilités (`Read-Only`)
- Inspecte les agendas et créneaux occupés sans limite d'horizon via `list-calendars`, `query-freebusy --from "<ISO>" --to "<ISO>"`, `list-events` et `hold-allowlist --list` (couvrant événements futurs lointains et séries récurrentes `RRULE`).
- Prend en charge les éventuelles extensions personnelles déclarées dans `instance/extensions/` sans aucune logique personnelle codée en dur dans `core/`.

## 2. Cycle `[HOLD]` Allowlisté, Réservations & Exclusion d'Auto-Invitation
1. **Règle Absolue Zéro Auto-Invitation Propriétaire ni Marchand** :
   - Les adresses personnelles et alias `sendAs` de l'utilisateur (`identity.default_send_as_email`, `identity.calendar_organizer_email`, `identity.never_invite_emails` et adresses authentifiées Gmail) ainsi que les adresses automatisées (`noreply`, `no-reply`, `feedback@`, `bounces+`) ne doivent **jamais** figurer dans `attendees` d'une invitation Google Calendar. Pour les confirmations d'achat/billets/réservations marchandes, conserver `attendees: []` et `--no-send-invites`.
2. **Pose de `[HOLD]` (`create-event --hold`)** :
   - Bloque l'agenda en `opaque` (`[HOLD] <Objet>`). Seuls les proches explicitement inscrits dans `calendar_hold_allowlist.json` sont ajoutés immédiatement ; tout interlocuteur externe est différé dans `alfred_candidate_attendees` (**zéro notification externe au stade `[HOLD]`**).
3. **Confirmation, Mise à Jour ou Suppression sous Gate Obligatoire (`implementation_plan.md`)** :
   - Toute conversion de `[HOLD]` (`confirm-event --send-invites` ou `--no-send-invites`, avec `--location` / `--description`), modification (`update-event`) ou suppression (`delete-event`) est soumise dans **`implementation_plan.md`** (`RequestFeedback: true`).

## 3. Étape 3 — Exécution Post-Validation (`plan-executor` — Modèle Léger `Hydrogen`)
- Après `Proceed`, `plan-executor` exécute la commande calendrier validée, nettoie les `[HOLD]` alternatifs du même fil et archive par défaut le fil Gmail source (sauf `--keep-in-inbox`).
- Toute mutation exécutée depuis un autre workspace surface automatiquement une trace visible (`EXECUTED`) dans `alfred-calendar` (dédupliquée lorsqu'exécutée déjà depuis `alfred-calendar`, sauf `--archive-trace` explicite).
