---
id: assistante_maternelle
title: Contrat Assistante Maternelle & Suivi Garde Nounou
domain: childcare
summary: Horaires contractuels, taux horaire net, indemnités entretien/repas, congés, préavis et référence contrat signé.
keywords:
  - nounou
  - assistante maternelle
  - garde
  - pajemploi
  - vacances
  - preavis
drive_source_ids: []
updated_at: "2026-01-01T00:00:00Z"
---

# Contrat Assistante Maternelle & Suivi Garde Nounou

## 1. Paramètres Contractuels Actifs
- Statut : En attente d'initialisation via `alfred init-nounou`.
- Calendrier Google dédié : `Garde Nounou`.
- Préavis contractuel congés : 30 jours calendaires avant le début des congés.

## 2. Routines Déterministes
- Le 20 du mois $M$ : pré-remplissage du mois $M+1$ (`seed-nounou`).
- À `last_day - 3` (février 28/29j inclus) : calcul déterministe Pajemploi (`close-pajemploi`).
