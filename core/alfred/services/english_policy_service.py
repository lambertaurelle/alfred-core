"""Deterministic English-only repository policy checker with bilingual runtime allowlist."""

from __future__ import annotations

import ast
import io
import tokenize
from pathlib import Path

from alfred.services.english_policy_helpers import (
    CODING_COMMENT_RE,
    check_clean_line,
    collapse_embedded_identifiers,
    collect_repo_scan_targets,
    extract_joined_str_parts,
    read_text_safe,
    strip_allowed_identifiers,
)

ALLOWED_TECHNICAL_IDENTIFIERS: frozenset[str] = frozenset(
    {
        "_ALFRED_REFERENTIEL_", "01_profil_et_contacts_cles.md", "profil_et_contacts_cles",
        "Archives_Froides", "fr-FR", "referentiel_drive_folder_name",
        "referentiel_folder_id", "referentiel_dir",
    }
)

BILINGUAL_RUNTIME_STRING_ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {
        ("core/alfred/services/inbox_booking_detector.py", "PASSIVE_PORTAL_PATTERNS"),
        ("core/alfred/services/inbox_booking_detector.py", "_AUTOMATED_EXACT_LOCALS"),
        ("core/alfred/services/inbox_booking_detector.py", "_AUTOMATED_LOCAL_RE"),
        ("core/alfred/services/inbox_booking_detector.py", "_SUBJECT_DISQUALIFY_RE"),
        ("core/alfred/services/inbox_booking_detector.py", "_PARCEL_DISQUALIFY_RE"),
        ("core/alfred/services/inbox_booking_detector.py", "_BOOKING_EVENT_CUE_RE"),
        ("core/alfred/services/inbox_booking_detector.py", "_BOOKING_CONFIRM_RE"),
        ("core/alfred/services/inbox_booking_detector.py", "_PURCHASE_CONFIRM_RE"),
        ("core/alfred/services/inbox_booking_detector.py", "_MONTH_WORDS"),
        ("core/alfred/services/inbox_booking_detector.py", "_STAY_OR_DEPART_RE"),
        ("core/alfred/services/inbox_eligibility.py", "CALENDAR_SUBJECT_RE"),
        ("core/alfred/services/email_security_service.py", "INJECTION_PATTERNS"),
        ("core/alfred/services/inbox_triage_service.py", "ACTION_REQUIRED_PATTERNS"),
        ("core/alfred/services/inbox_triage_service.py", "COMPLEX_STUDY_PATTERNS"),
        ("core/alfred/services/inbox_triage_service.py", "DRIVE_SEARCH_STOPWORDS"),
        ("core/alfred/services/knowledge_service.py", "QUERY_STOPWORDS"),
        ("core/alfred/domain/drive_watcher_models.py", "DEFAULT_EXCLUDED_DRIVE_PATH_PREFIXES"),
        ("core/alfred/services/attachment_extractor.py", "ATTACHMENT_BODY_MARKERS"),
        ("core/alfred/services/core_spec_service.py", "_BANNED_V7_TERMS"),
        ("core/alfred/services/english_policy_service.py", "ALLOWED_TECHNICAL_IDENTIFIERS"),
        ("core/alfred/services/english_policy_service.py", "BILINGUAL_RUNTIME_STRING_ALLOWLIST"),
        ("core/alfred/services/english_policy_helpers.py", "ENGLISH_IDIOM_RE"),
        ("core/alfred/services/english_policy_helpers.py", "FRENCH_DIACRITIC_RE"),
        ("core/alfred/services/english_policy_helpers.py", "FRENCH_ELISION_RE"),
        ("core/alfred/services/english_policy_helpers.py", "FRENCH_PHRASE_RE"),
        ("core/alfred/services/english_policy_helpers.py", "FRENCH_STRONG_WORDS"),
        ("core/alfred/services/english_policy_helpers.py", "FRENCH_COMMON_WORDS"),
    }
)


def detect_french_in_text(text: str) -> list[str]:
    """Detect French diacritics, contractions, strong words, or stopword clusters in text.

    Args:
        text: Input string from documentation, comments, docstrings, or UI literals.

    Returns:
        List of human-readable French policy violation descriptions (empty when clean).
    """
    if not text:
        return []
    cleaned = strip_allowed_identifiers(text, ALLOWED_TECHNICAL_IDENTIFIERS)
    violations: list[str] = []
    for line_no, raw_line in enumerate(cleaned.splitlines(), 1):
        hit = check_clean_line(raw_line, line_no)
        if hit is not None:
            violations.append(hit)
            continue
        collapsed = collapse_embedded_identifiers(raw_line, ALLOWED_TECHNICAL_IDENTIFIERS)
        if collapsed != raw_line:
            splice_hit = check_clean_line(collapsed, line_no)
            if splice_hit is not None:
                violations.append(splice_hit)
    return violations


def validate_english_text(text: str, label: str = "text") -> str | None:
    """Validate that text contains zero French prose, raising ValueError on violation.

    Args:
        text: Text string to validate.
        label: Descriptive label included in the ValueError message.

    Returns:
        None when the text complies with the English-only policy.

    Raises:
        ValueError: If any French text is detected.
    """
    hits = detect_french_in_text(text)
    if hits:
        raise ValueError(f"French text is forbidden in {label}: {'; '.join(hits)}")
    return None


def _collect_exempt_node_ids(tree: ast.Module, rel_path: str) -> set[int]:
    """Collect AST node IDs for the first top-level assignment of each allowlisted symbol."""
    allowlisted = {
        sym for path_key, sym in BILINGUAL_RUNTIME_STRING_ALLOWLIST if path_key == rel_path
    }
    exempt_ids: set[int] = set()
    if not allowlisted:
        return exempt_ids
    seen_syms: set[str] = set()
    for stmt in tree.body:
        target_names: list[str] = []
        value_node: ast.expr | None = None
        if isinstance(stmt, ast.Assign):
            if all(isinstance(t, ast.Name) for t in stmt.targets):
                target_names = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
                value_node = stmt.value
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            target_names = [stmt.target.id]
            value_node = stmt.value
        if (
            value_node is not None
            and target_names
            and all(name in allowlisted and name not in seen_syms for name in target_names)
        ):
            seen_syms.update(target_names)
            for child in ast.walk(value_node):
                if isinstance(child, ast.Constant) and isinstance(child.value, (str, bytes)):
                    exempt_ids.add(id(child))
    return exempt_ids


def _collect_docstrings(tree: ast.Module) -> tuple[set[int], list[tuple[int, str]]]:
    """Collect docstring AST Constant IDs and (lineno, docstring_text) pairs."""
    doc_ids: set[int] = set()
    docs: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        if not node.body:
            continue
        first = node.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            doc_ids.add(id(first.value))
            docs.append((first.lineno, first.value.value))
    return doc_ids, docs


def _inspect_python_comments(source: str, rel_path: str) -> list[str]:
    """Scan all Python # comments via tokenize with strict line 1-2 header exemptions."""
    violations: list[str] = []
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError):
        return violations
    for tok in tokens:
        if tok.type != tokenize.COMMENT:
            continue
        is_shebang = tok.start == (1, 0) and tok.string.startswith("#!")
        is_coding = (
            tok.start[0] <= 2
            and tok.start[1] == 0
            and bool(CODING_COMMENT_RE.match(tok.string))
        )
        if is_shebang and not detect_french_in_text(tok.string[2:].strip()):
            continue
        if is_coding:
            continue
        comment_body = tok.string.lstrip("#").strip()
        for hit in detect_french_in_text(comment_body):
            violations.append(f"{rel_path}:{tok.start[0]} (comment): {hit}")
    return violations


def _inspect_non_test_literals(
    tree: ast.Module,
    rel_path: str,
    doc_ids: set[int],
) -> list[str]:
    """Inspect non-docstring string/bytes constants and f-strings outside allowlisted symbols."""
    violations: list[str] = []
    exempt_ids = _collect_exempt_node_ids(tree, rel_path)
    joined_part_ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        extracted = extract_joined_str_parts(node, exempt_ids)
        if extracted is None:
            continue
        tight_text, spaced_text, part_ids = extracted
        joined_part_ids.update(part_ids)
        seen_hits: set[str] = set()
        for candidate in (tight_text, spaced_text):
            for hit in detect_french_in_text(candidate):
                if hit not in seen_hits:
                    seen_hits.add(hit)
                    violations.append(f"{rel_path}:{node.lineno} (f-string): {hit}")

    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant):
            continue
        if id(node) in doc_ids or id(node) in exempt_ids or id(node) in joined_part_ids:
            continue
        if isinstance(node.value, str):
            text_val, kind = node.value, "string literal"
        elif isinstance(node.value, bytes):
            text_val, kind = node.value.decode("utf-8", errors="ignore"), "bytes literal"
        else:
            continue
        for hit in detect_french_in_text(text_val):
            violations.append(f"{rel_path}:{node.lineno} ({kind}): {hit}")
    return violations


def inspect_python_source(
    source: str,
    rel_path: str,
    *,
    is_test_file: bool = False,
) -> list[str]:
    """Inspect Python source comments, docstrings, and literals for French policy compliance.

    Args:
        source: UTF-8 Python source code string.
        rel_path: Repository-relative POSIX path used for allowlist matching.
        is_test_file: When True, permits French runtime fixtures while checking docs/comments.

    Returns:
        List of violation strings (empty when compliant).
    """
    violations = _inspect_python_comments(source, rel_path)
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return violations

    doc_ids, docs = _collect_docstrings(tree)
    for lineno, doc_text in docs:
        for hit in detect_french_in_text(doc_text):
            violations.append(f"{rel_path}:{lineno} (docstring): {hit}")

    if not is_test_file:
        violations.extend(_inspect_non_test_literals(tree, rel_path, doc_ids))
    return violations


def inspect_repository_english(repo_root: Path) -> list[str]:
    """Scan repository docs, templates, configs, hooks, and Python sources for French prose.

    Args:
        repo_root: Root directory of the Alfred repository or isolated git worktree.

    Returns:
        List of human-readable violation strings (empty when the repository is compliant).
    """
    root = repo_root.resolve()
    text_files, py_targets = collect_repo_scan_targets(root)
    violations: list[str] = []
    seen_text: set[Path] = set()
    for text_path in text_files:
        rel = text_path.relative_to(root).as_posix()
        resolved = text_path.resolve()
        if text_path.exists() and not resolved.is_relative_to(root):
            violations.append(f"{rel}: symlink target escapes repository root")
            continue
        if resolved not in seen_text and (content := read_text_safe(text_path)) is not None:
            seen_text.add(resolved)
            violations.extend(f"{rel}: {hit}" for hit in detect_french_in_text(content))

    seen_py: set[Path] = set()
    for py_path, is_test in py_targets:
        rel = py_path.relative_to(root).as_posix()
        resolved = py_path.resolve()
        if py_path.exists() and not resolved.is_relative_to(root):
            violations.append(f"{rel}: symlink target escapes repository root")
            continue
        if resolved not in seen_py and (content := read_text_safe(py_path)) is not None:
            seen_py.add(resolved)
            violations.extend(inspect_python_source(content, rel, is_test_file=is_test))
    return violations
