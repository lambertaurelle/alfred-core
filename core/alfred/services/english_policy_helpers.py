"""Low-level text normalization, French pattern matching, and repo file collection helpers."""

from __future__ import annotations

import ast
import re
import unicodedata
from pathlib import Path

ENGLISH_IDIOM_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    r"c['’]mon|"
    r"s['’]mores|"
    r"coup\s+d['’]etat|"
    r"l['’]oreal|"
    r"d['’]angelo"
    r")(?![A-Za-z0-9_])",
    re.IGNORECASE,
)

FRENCH_DIACRITIC_RE = re.compile(r"[àâäçéèêëîïôöùûüÿœæÀÂÄÇÉÈÊËÎÏÔÖÙÛÜŸŒÆ«»]")
FRENCH_ELISION_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    r"(?:jusqu|lorsqu|puisqu)['’][aàa-z]*|"
    r"(?:l|d|qu|n|c|j|s|m|t)['’](?:e-mail|a|[a-z]{2,})\b"
    r")",
    re.IGNORECASE,
)
FRENCH_PHRASE_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    r"mise\s+a\s+jour(?![A-Za-z0-9_])|"
    r"(?:correction|suppression|modification|ajout|gestion)\s+(?:du|de|des|d['’][a-z]+)(?![A-Za-z0-9_])|"
    r"regles\s+(?:de|du|des)(?![A-Za-z0-9_])|"
    r"cache\s+local(?![A-Za-z0-9_])|"
    r"tout\s+est(?![A-Za-z0-9_])"
    r")",
    re.IGNORECASE,
)
WORD_TOKEN_RE = re.compile(r"[a-z0-9_]+(?:-[a-z0-9_]+)*")

FRENCH_STRONG_WORDS: frozenset[str] = frozenset(
    {
        "uniquement", "obligatoire", "obligatoires", "brouillon", "brouillons",
        "utilisateur", "utilisateurs", "arborescence", "defrichage", "mainteneur",
        "echeance", "echeances", "expediteur", "destinataire", "destinataires",
        "classement", "fichiers", "fichier", "taches", "tache", "creneau", "creneaux",
        "unitaire", "unitaires", "fonctionnelle", "fonctionnelles", "generique", "generiques",
        "personnelle", "personnelles", "aucun", "aucune", "toujours", "jamais",
        "lorsque", "sinon", "depuis", "votre", "notre", "cette", "chaque", "selon",
        "etapes", "etape", "prochaines", "prochaine", "prochain", "inconnue", "inconnu",
        "jointe", "jointes", "rangement", "cartographie", "inventaire", "couche",
        "commandes", "commande", "defaut", "existants", "existant", "existante",
        "valider", "executer", "soumettre", "recommande", "recommandee", "recommandees",
        "interdite", "interdit", "autorisee", "autorise", "approuvee", "approuve",
        "introuvable", "lancee", "courtois", "courtoise", "synthetique", "horaire", "horaires",
        "prealable", "manuscrite", "hors-bande", "conserver", "regles", "regle",
        "francais", "francaise", "requis", "requise", "ajout", "ajouter",
        "bonjour", "merci", "cordialement", "madame", "monsieur", "classifieur",
        "synthese", "donnees", "autorisation", "administrateur", "autoriser",
        "statut", "parametre", "parametres", "maintenant", "racine",
        "fonctionnalite", "fonctionnalites", "jour", "jours",
        "nouveau", "nouvelle", "nouveaux", "nouvelles",
        "sauvegarde", "nettoyage", "demarrage", "affichage",
        "enregistrement", "enregistrements", "bientot", "egalement", "supprimer",
    }
)

FRENCH_COMMON_WORDS: frozenset[str] = frozenset(
    {
        "les", "des", "une", "pour", "avec", "dans", "sans", "qui", "que", "sont",
        "tout", "tous", "toute", "toutes", "vos", "nos", "cet", "ces", "du", "au",
        "puis", "avant", "apres", "vers", "hors", "pas", "comme", "aussi",
        "peut", "doit", "etre", "avoir", "faire", "humaine", "texte",
        "tete", "referentiel", "fiche", "fiches",
        "titre", "suivi", "envoi", "envoyer", "creer", "deux", "trois",
    }
)

SKIP_DIRS = frozenset(
    {".git", ".venv", "__pycache__", ".mypy_cache", ".pytest_cache", "worktrees"}
)
BINARY_EXTENSIONS: frozenset[str] = frozenset(
    {".jpg", ".jpeg", ".png", ".gif", ".webp", ".ico", ".pdf", ".pyc", ".pyo",
     ".so", ".bin", ".db", ".sqlite", ".zip", ".tar", ".gz", ".woff", ".woff2"}
)
CODING_COMMENT_RE = re.compile(
    r"^#\s*-\*-\s*coding[:=]\s*[-\w.]+\s*-\*-\s*$|^#\s*coding[:=]\s*[-\w.]+\s*$"
)


def strip_allowed_identifiers(
    text: str,
    allowed_identifiers: frozenset[str],
) -> str:
    """Normalize text to NFC, drop invisible chars, and mask identifiers and English idioms.

    Args:
        text: Raw input text to sanitize prior to French detection.
        allowed_identifiers: Boundary-matched technical identifiers permitted in English docs.

    Returns:
        Normalized text with invisible format chars stripped and allowlisted tokens masked.
    """
    normalized = unicodedata.normalize("NFC", text)
    normalized = "".join(
        ch for ch in normalized if unicodedata.category(ch) != "Cf" and ch != "\u00ad"
    )
    for ident in sorted(allowed_identifiers, key=len, reverse=True):
        pattern = rf"(?<![A-Za-z0-9_]){re.escape(ident)}(?![A-Za-z0-9_])"
        normalized = re.sub(pattern, " ", normalized)
    return ENGLISH_IDIOM_RE.sub(" ", normalized)


def collapse_embedded_identifiers(
    line: str,
    allowed_identifiers: frozenset[str],
) -> str:
    """Collapse allowlisted identifiers spliced directly inside alphanumeric tokens.

    Args:
        line: Already boundary-masked line of text.
        allowed_identifiers: Allowlisted identifiers to remove when embedded mid-token.

    Returns:
        Line with any remaining embedded allowlisted identifiers removed without spacing.
    """
    collapsed = line
    for ident in sorted(allowed_identifiers, key=len, reverse=True):
        if ident in collapsed:
            collapsed = collapsed.replace(ident, "")
    return collapsed


def check_clean_line(raw_line: str, line_no: int) -> str | None:
    """Inspect one normalized line for French diacritics, contractions, phrases, or vocabulary.

    Args:
        raw_line: Preprocessed single line of text.
        line_no: 1-based line number for violation reporting.

    Returns:
        Human-readable violation string, or None when the line is compliant.
    """
    if FRENCH_DIACRITIC_RE.search(raw_line):
        return f"line {line_no}: French diacritic or guillemet detected"
    if FRENCH_ELISION_RE.search(raw_line):
        return f"line {line_no}: French elided contraction detected"
    if FRENCH_PHRASE_RE.search(raw_line):
        return f"line {line_no}: French phrase detected"
    tokens = WORD_TOKEN_RE.findall(raw_line.lower())
    strong_hits = [tok for tok in tokens if tok in FRENCH_STRONG_WORDS]
    if strong_hits:
        return f"line {line_no}: French word '{strong_hits[0]}' detected"
    common_hits = [tok for tok in tokens if tok in FRENCH_COMMON_WORDS]
    if len(common_hits) >= 2:
        sample = ", ".join(sorted(set(common_hits))[:3])
        return f"line {line_no}: French words ({sample}) detected"
    return None


def extract_joined_str_parts(
    node: ast.JoinedStr,
    exempt_ids: set[int],
) -> tuple[str, str, set[int]] | None:
    """Build tight and space-padded candidate strings from an AST f-string node.

    Args:
        node: Parsed ast.JoinedStr expression node.
        exempt_ids: Set of AST Constant node IDs exempt via bilingual allowlist.

    Returns:
        Tuple of (tight_text, spaced_text, part_constant_ids), or None when exempt.
    """
    tight_chunks: list[str] = []
    spaced_chunks: list[str] = []
    part_ids: set[int] = set()
    for part in node.values:
        if isinstance(part, ast.Constant) and isinstance(part.value, str):
            if id(part) in exempt_ids:
                return None
            part_ids.add(id(part))
            tight_chunks.append(part.value)
            spaced_chunks.append(part.value)
        elif (
            isinstance(part, ast.FormattedValue)
            and isinstance(part.value, ast.Constant)
            and isinstance(part.value.value, str)
        ):
            if id(part.value) in exempt_ids:
                return None
            part_ids.add(id(part.value))
            tight_chunks.append(part.value.value)
            spaced_chunks.append(part.value.value)
        else:
            spaced_chunks.append(" ")
    return "".join(tight_chunks), " ".join(spaced_chunks), part_ids


def read_text_safe(path: Path) -> str | None:
    """Safely read UTF-8 text from an existing non-broken file, replacing invalid bytes.

    Args:
        path: File system path to read.

    Returns:
        Decoded UTF-8 text string, or None for broken symlinks, binaries, or unreadable files.
    """
    try:
        if path.is_symlink() and not path.exists():
            return None
        if not path.is_file() or path.suffix.lower() in BINARY_EXTENSIONS:
            return None
        return path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return None


def iter_glob_files(base_dir: Path, pattern: str) -> list[Path]:
    """Yield sorted matching regular files while skipping ignored directories and broken links.

    Args:
        base_dir: Directory subtree root to search.
        pattern: Glob pattern passed to Path.rglob().

    Returns:
        Sorted list of matching file paths.
    """
    if not base_dir.is_dir():
        return []
    found: list[Path] = []
    for item in sorted(base_dir.rglob(pattern)):
        if any(part in SKIP_DIRS for part in item.relative_to(base_dir).parts):
            continue
        if item.is_symlink() and not item.exists():
            continue
        if item.is_file():
            found.append(item)
    return found


def collect_repo_scan_targets(root: Path) -> tuple[list[Path], list[tuple[Path, bool]]]:
    """Collect text documentation/config targets and (python_path, is_test) targets.

    Args:
        root: Resolved repository root directory path.

    Returns:
        Tuple of (text_paths, python_targets_with_is_test_flag).
    """
    single_texts = [
        root / "README.md",
        root / "AGENTS.md",
        root / "pyproject.toml",
        root / ".gitignore",
        root / ".pre-commit-config.yaml",
        root / ".agents" / "hooks.json",
        root / "instance" / "extensions" / "README.md",
        *(sorted((root / "core" / "workspaces").glob("*/AGENTS.md"))),
    ]
    text_files: list[Path] = [p for p in single_texts if p.exists() and p.is_file()]
    if (hooks_dir := root / ".githooks").is_dir():
        text_files.extend(
            h for h in sorted(hooks_dir.iterdir())
            if h.exists() and h.is_file() and h.suffix.lower() not in BINARY_EXTENSIONS
        )
    for folder, patterns in (
        (root / "docs", ("*.md", "*.txt", "*.yaml", "*.yml")),
        (root / ".github", ("*.md", "*.yaml", "*.yml")),
        (root / ".agents" / "agents", ("*.md",)),
        (root / "scripts", ("*.sh",)),
        (root / "core" / "scripts", ("*.sh",)),
        (root / "core" / "templates", ("*.md",)),
        (root / "core" / "config", ("*.yaml", "*.yml", "*.md")),
        (root / "instance" / "config", ("*.example.*",)),
        (root / "instance" / "examples", ("*.yaml", "*.yml", "*.md")),
    ):
        for pat in patterns:
            text_files.extend(iter_glob_files(folder, pat))

    py_targets: list[tuple[Path, bool]] = [
        (p, is_test)
        for py_dir, is_test in (
            (root / "core" / "alfred", False),
            (root / "core" / "scripts", False),
            (root / "core" / "tests", True),
        )
        for p in iter_glob_files(py_dir, "*.py")
    ]
    py_targets.extend((p, False) for p in iter_glob_files(root / "instance" / "examples", "*.py"))
    py_targets.extend(
        (p, False) for p in iter_glob_files(root / "instance" / "extensions", "*.example.py")
    )
    inst_init = root / "instance" / "extensions" / "__init__.py"
    if inst_init.exists() and inst_init.is_file():
        py_targets.append((inst_init, False))
    return text_files, py_targets
