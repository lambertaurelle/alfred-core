---
id: drive_taxonomy_and_filing_rules
title: Google Drive Taxonomy & Filing Rules
domain: drive_governance
summary: Google Drive folder hierarchy taxonomy, filing conventions, and first-run indexing (init-drive).
keywords:
  - drive
  - taxonomy
  - filing
  - hierarchy
  - folders
drive_source_ids: []
updated_at: "2026-01-01T00:00:00Z"
---

# Google Drive Taxonomy & Filing Rules

## 1. Detected Root Folders
- `_ALFRED_REFERENTIEL_`: One-way backup of `.md` knowledge reference sheets with YAML frontmatter.
- Domain folders automatically indexed during `alfred init-drive`.

## 2. Filing Conventions
- Original supporting documents (PDF contracts, invoices, certificates) remain in their primary Google Drive folders.
- Up-to-date summaries are maintained in `instance/data/knowledge/*.md` with `drive_source_ids` pointers.
