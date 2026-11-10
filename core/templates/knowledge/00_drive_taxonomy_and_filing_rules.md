---
id: drive_taxonomy_and_filing_rules
title: Cartographie Google Drive & Règles de Rangement
domain: drive_governance
summary: Taxonomie de l'arborescence Google Drive, conventions de classement et indexation First-Run (init-drive).
keywords:
  - drive
  - taxonomie
  - rangement
  - arborescence
  - classement
drive_source_ids: []
updated_at: "2026-01-01T00:00:00Z"
---

# Cartographie Google Drive & Règles de Rangement

## 1. Dossiers Racines Détectés
- `_ALFRED_REFERENTIEL_` : Sauvegarde unidirectionnelle des fiches de référence `.md` à Frontmatter YAML.
- Dossiers thématiques détectés automatiquement lors de `alfred init-drive`.

## 2. Conventions de Classement
- Les pièces justificatives originales (contrats PDF, factures, attestations) restent dans leurs dossiers Google Drive d'origine.
- Les synthèses à jour sont consolidées dans `instance/data/knowledge/*.md` avec pointeurs `drive_source_ids`.
