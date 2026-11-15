"""3-Tier Progressive Knowledge loader (YAML Frontmatter -> Reference .md -> Drive backup)."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import yaml

from alfred.domain.models import (
    AlfredPaths,
    AlfredSettings,
    FrontmatterSchema,
    KnowledgeAuditEvent,
    KnowledgeDocument,
    fold_accents,
    utc_now_iso,
)
from alfred.repositories.json_repository import atomic_write_text
from alfred.repositories.state_store import StateStore

QUERY_STOPWORDS: frozenset[str] = frozenset(
    {
        "par", "sur", "mon", "mes", "ton", "tes", "son", "ses", "nos", "vos", "une", "des",
        "les", "aux", "est", "sont", "ete", "avoir", "etre", "pour", "dans", "avec", "sans",
        "tout", "tous", "toute", "toutes", "plus", "moins", "vous", "nous", "leur", "leurs",
        "cet", "cette", "ces", "qui", "que", "quoi", "dont", "ou", "mais", "donc", "car",
        "bonjour", "merci", "cordialement", "bien", "madame", "monsieur", "objet", "mail",
        "message", "nouveau", "nouvelle", "suite", "votre", "notre", "the", "and", "for",
        "with", "from", "this", "that", "your", "have", "are", "was", "were", "will",
    }
)


class KnowledgeService:
    """Manage Tier-1 YAML frontmatter scanning, Tier-2 .md loading, and Drive .md backups."""

    def __init__(
        self,
        paths: AlfredPaths,
        settings: AlfredSettings,
        state_store: StateStore,
        backup_fn: Callable[[str, str], str] | None = None,
    ) -> None:
        """Initialize the 3-tier knowledge service.

        Args:
            paths: Resolved AlfredPaths filesystem layout.
            settings: Runtime structural settings (including max_knowledge_body_lines).
            state_store: Local atomic state store for audit logging.
            backup_fn: Optional callback uploading updated .md files to _ALFRED_REFERENTIEL_.
        """
        self._paths = paths
        self._settings = settings
        self._state_store = state_store
        self._backup_fn = backup_fn
        self._paths.knowledge_dir.mkdir(parents=True, exist_ok=True)

    def bootstrap_templates_if_empty(self) -> list[str]:
        """Seed missing reference .md files ONLY when knowledge_dir has zero .md files yet.

        Returns:
            List of seeded template file names.
        """
        seeded: list[str] = []
        if list(self._paths.knowledge_dir.glob("*.md")):
            return seeded
        if not self._paths.templates_knowledge_dir.exists():
            return seeded
        for tpl in sorted(self._paths.templates_knowledge_dir.glob("*.md")):
            dest = self._paths.knowledge_dir / tpl.name
            if not dest.exists():
                atomic_write_text(dest, tpl.read_text(encoding="utf-8"))
                seeded.append(tpl.name)
        return seeded

    def scan_frontmatters(self) -> list[tuple[str, FrontmatterSchema]]:
        """Tier-1 Progressive Disclosure: read ONLY YAML frontmatter headers (~50 tokens).

        Returns:
            Sorted list of (file_name, FrontmatterSchema) pairs without loading bodies.
        """
        results: list[tuple[str, FrontmatterSchema]] = []
        for md_path in sorted(self._paths.knowledge_dir.glob("*.md")):
            fm = self._read_frontmatter_only(md_path)
            results.append((md_path.name, fm))
        return results

    def select_relevant_frontmatters(self, query: str) -> list[tuple[str, FrontmatterSchema]]:
        """Rank reference frontmatters using stopword-filtered whole-word/stem matching.

        Args:
            query: User query, email subject/snippet, or topic string.

        Returns:
            Matching (file_name, FrontmatterSchema) pairs ordered by relevance score descending.
        """
        folded_query = fold_accents(query)
        tokens = {
            tok
            for tok in re.findall(r"[a-z0-9_]{3,}", folded_query)
            if tok not in QUERY_STOPWORDS and not tok.isdigit()
        }
        if not tokens:
            return []
        scored: list[tuple[int, str, FrontmatterSchema]] = []
        for file_name, fm in self.scan_frontmatters():
            kw_words = {
                w
                for kw in fm.keywords
                for w in re.findall(r"[a-z0-9_]{3,}", fold_accents(kw))
            }
            meta_words = set(
                re.findall(
                    r"[a-z0-9_]{3,}",
                    fold_accents(" ".join([fm.id, fm.title, fm.domain, fm.summary])),
                )
            )
            score = 0
            for tok in tokens:
                if tok in kw_words or (len(tok) >= 5 and any(w.startswith(tok) for w in kw_words)):
                    score += 3
                elif tok in meta_words or (
                    len(tok) >= 5 and any(w.startswith(tok) for w in meta_words)
                ):
                    score += 1
            if score > 0:
                scored.append((score, file_name, fm))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [(fname, fm) for _, fname, fm in scored]

    def load_document(self, doc_id_or_filename: str) -> KnowledgeDocument:
        """Tier-2 Progressive Disclosure: load full .md reference file (<= 150 body lines).

        Args:
            doc_id_or_filename: Frontmatter id or Markdown file name.

        Returns:
            Parsed KnowledgeDocument with validated FrontmatterSchema and concise body.

        Raises:
            FileNotFoundError: If no reference file matches doc_id_or_filename.
            ValueError: If the Markdown body exceeds max_knowledge_body_lines.
        """
        target = self._resolve_doc_path(doc_id_or_filename)
        frontmatter, body = self._parse_full_markdown(target)
        body_lines = len(body.splitlines())
        if body_lines > self._settings.max_knowledge_body_lines:
            raise ValueError(
                f"Reference file '{target.name}' has {body_lines} body lines "
                f"(limit: {self._settings.max_knowledge_body_lines})."
            )
        return KnowledgeDocument(
            frontmatter=frontmatter,
            body=body,
            file_name=target.name,
            body_line_count=body_lines,
        )

    def upsert_document(
        self,
        file_name: str,
        frontmatter: FrontmatterSchema,
        body: str,
    ) -> KnowledgeDocument:
        """Atomically save an up-to-date .md reference file and back it up to Google Drive.

        Args:
            file_name: Target .md file name under instance/data/knowledge/.
            frontmatter: Validated YAML frontmatter metadata.
            body: Concise Markdown synthesis body (must be <= max_knowledge_body_lines).

        Returns:
            Persisted KnowledgeDocument with refreshed updated_at timestamp.

        Raises:
            ValueError: If body exceeds the configured line limit (default 150 lines).
        """
        if Path(file_name).name != file_name or not file_name.endswith(".md"):
            raise ValueError(f"Invalid reference file name: '{file_name}' (must be '<name>.md').")
        normalized_body = body.replace("\r\n", "\n").strip() + "\n"
        body_lines = len(normalized_body.splitlines())
        if body_lines > self._settings.max_knowledge_body_lines:
            raise ValueError(
                f"Cannot save '{file_name}': {body_lines} body lines exceed "
                f"limit of {self._settings.max_knowledge_body_lines}."
            )
        updated_fm = frontmatter.model_copy(update={"updated_at": utc_now_iso()})
        yaml_header = yaml.safe_dump(
            updated_fm.model_dump(mode="json"),
            sort_keys=False,
            allow_unicode=True,
        ).strip()
        raw_markdown = f"---\n{yaml_header}\n---\n\n{normalized_body}"

        dest = self._paths.knowledge_dir / file_name
        atomic_write_text(dest, raw_markdown)

        backup_id = ""
        if self._backup_fn is not None and self._settings.features.drive_knowledge_sync:
            backup_id = self._backup_fn(file_name, raw_markdown)

        self._state_store.knowledge_audit.append(
            KnowledgeAuditEvent(
                doc_id=updated_fm.id,
                file_name=file_name,
                action="UPSERT",
                drive_backup_file_id=backup_id,
            )
        )
        return KnowledgeDocument(
            frontmatter=updated_fm,
            body=normalized_body,
            file_name=file_name,
            body_line_count=body_lines,
        )

    def _resolve_doc_path(self, doc_id_or_filename: str) -> Path:
        """Locate a knowledge .md file by exact file name, stem, or frontmatter id."""
        cleaned = doc_id_or_filename.strip()
        if cleaned and Path(cleaned).name == cleaned and cleaned not in {".", ".."}:
            direct = self._paths.knowledge_dir / cleaned
            if direct.is_file():
                return direct
            if not cleaned.endswith(".md"):
                with_ext = self._paths.knowledge_dir / f"{cleaned}.md"
                if with_ext.is_file():
                    return with_ext
        if cleaned:
            for md_path in sorted(self._paths.knowledge_dir.glob("*.md")):
                fm = self._read_frontmatter_only(md_path)
                if fm.id == cleaned:
                    return md_path
        raise FileNotFoundError(f"Knowledge document '{doc_id_or_filename}' not found.")

    def _read_frontmatter_only(self, md_path: Path) -> FrontmatterSchema:
        """Read only YAML lines up to the second '---' delimiter without reading the body."""
        yaml_lines: list[str] = []
        with md_path.open("r", encoding="utf-8") as handle:
            first = handle.readline()
            if first.strip() != "---":
                raise ValueError(f"Missing opening YAML frontmatter delimiter in {md_path.name}")
            for line in handle:
                if line.strip() == "---":
                    break
                yaml_lines.append(line)
            else:
                raise ValueError(f"Missing closing YAML frontmatter delimiter in {md_path.name}")
        parsed = yaml.safe_load("".join(yaml_lines)) or {}
        return FrontmatterSchema.model_validate(parsed)

    def _parse_full_markdown(self, md_path: Path) -> tuple[FrontmatterSchema, str]:
        """Split a Markdown file into validated FrontmatterSchema and Markdown body."""
        lines = md_path.read_text(encoding="utf-8").replace("\r\n", "\n").splitlines(keepends=True)
        if not lines or lines[0].strip() != "---":
            raise ValueError(f"Missing opening YAML frontmatter delimiter in {md_path.name}")
        closing_idx: int | None = None
        for idx in range(1, len(lines)):
            if lines[idx].strip() == "---":
                closing_idx = idx
                break
        if closing_idx is None:
            raise ValueError(f"Missing closing YAML frontmatter delimiter in {md_path.name}")
        fm_raw = yaml.safe_load("".join(lines[1:closing_idx])) or {}
        body = "".join(lines[closing_idx + 1 :]).lstrip("\n")
        return FrontmatterSchema.model_validate(fm_raw), body
