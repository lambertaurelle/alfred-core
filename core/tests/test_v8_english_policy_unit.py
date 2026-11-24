"""Unit tests for English-only repository policy, PR metadata guard, and attachment splitting."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_detect_french_in_text_catches_diacritics_nfd_contractions_and_unaccented_prose() -> None:
    """Verify detect_french_in_text catches accented, NFD, elided, and unaccented French."""
    from alfred.services.english_policy_service import detect_french_in_text

    samples = [
        "Résumé des règles de classement",
        "Bonjour « utilisateur » et à bientôt",
        "Cœur d'œuvre et façade",
        "Cafe\u0301 et Re\u0301fe\u0301rentiel",
        "Guide d'e\u0301volution pour l'utilisateur",
        "Verification pour l'utilisateur et d'evolution qu'aucun test ne rate",
        "Verification des regles de classement pour les fichiers",
        "Arbitrage mainteneur requis",
    ]
    for text in samples:
        hits = detect_french_in_text(text)
        assert hits, f"Expected French detection on: {text!r}"


def test_detect_french_in_text_allows_english_symbols_and_technical_identifiers() -> None:
    """Verify detect_french_in_text produces zero false positives on English and allowlisted IDs."""
    from alfred.services.english_policy_service import (
        ALLOWED_TECHNICAL_IDENTIFIERS,
        detect_french_in_text,
    )

    expected_identifiers = (
        "_ALFRED_REFERENTIEL_",
        "01_profil_et_contacts_cles.md",
        "profil_et_contacts_cles",
        "Archives_Froides",
        "fr-FR",
        "referentiel_drive_folder_name",
        "referentiel_folder_id",
    )
    for ident in expected_identifiers:
        assert ident in ALLOWED_TECHNICAL_IDENTIFIERS

    clean_samples = [
        "Clean English technical prose for verification, decision, extension, and architecture.",
        "Stage 1 -> Stage 2 -> Stage 3 — deterministic verification ✅ ⚠️ 🎩",
        "Backup Markdown knowledge files into _ALFRED_REFERENTIEL_ on Google Drive.",
        "Fallback profile file is 01_profil_et_contacts_cles.md with id profil_et_contacts_cles.",
        "Skip /Archives_Froides and configure locale fr-FR when needed.",
        "Settings expose referentiel_drive_folder_name and referentiel_folder_id.",
    ]
    for text in clean_samples:
        assert detect_french_in_text(text) == [], f"Unexpected false positive on: {text!r}"


def test_validate_english_text_accepts_english_and_rejects_french() -> None:
    """Verify validate_english_text returns None on English and raises ValueError on French."""
    from alfred.services.english_policy_service import validate_english_text

    assert validate_english_text("All 5 deterministic gates passed -> ready — ✅.") is None

    for bad in (
        "Résumé des modifications",
        "Re\u0301fe\u0301rentiel mis à jour",
        "Regles pour l'utilisateur",
        "Verification des regles de classement pour les fichiers",
        "Arbitrage mainteneur requis",
    ):
        with pytest.raises(ValueError):
            validate_english_text(bad)


def test_inspect_python_source_rejects_french_docstrings_and_comments_without_exemptions() -> None:
    """Verify AST docstrings and comments forbid French even inside bilingual runtime modules."""
    from alfred.services.english_policy_service import inspect_python_source

    clean_src = (
        '"""Clean English module docstring."""\n\n'
        "# Clean English comment\n"
        'class CleanClass:\n    """Clean class docstring."""\n\n'
        '    def run(self) -> str:\n        """Clean method docstring."""\n        return "ok"\n'
    )
    assert inspect_python_source(clean_src, "core/alfred/services/clean_svc.py") == []

    bad_sources = [
        '"""Module de résumé français."""\n',
        '"""English module."""\n\nclass Demo:\n    """Classe de vérification."""\n',
        '"""English module."""\n\ndef fn() -> None:\n    """Fonction pour les fichiers."""\n',
        '"""English module."""\n\n# Commentaire en français pour les tests\nX = 1\n',
        '"""Module pour l\'utilisateur."""\n\nPASSIVE_PORTAL_PATTERNS = ("newsletter",)\n',
        '"""English module."""\n\n# Regle pour les utilisateurs\nQUERY_STOPWORDS = frozenset()\n',
    ]
    paths = [
        "core/alfred/services/demo.py",
        "core/alfred/services/demo.py",
        "core/alfred/services/demo.py",
        "core/alfred/services/demo.py",
        "core/alfred/services/inbox_booking_detector.py",
        "core/alfred/services/knowledge_service.py",
    ]
    for src, rel_path in zip(bad_sources, paths, strict=True):
        violations = inspect_python_source(src, rel_path)
        assert violations, f"Expected violation for {rel_path}:\n{src}"


def test_inspect_python_source_bilingual_allowlist_and_non_allowlisted_literals() -> None:
    """Verify BILINGUAL_RUNTIME_STRING_ALLOWLIST permits only exact (rel_path, symbol) pairs."""
    from alfred.services.english_policy_service import (
        BILINGUAL_RUNTIME_STRING_ALLOWLIST,
        inspect_python_source,
    )

    expected_pairs = [
        ("core/alfred/services/inbox_booking_detector.py", "PASSIVE_PORTAL_PATTERNS"),
        ("core/alfred/services/inbox_eligibility.py", "CALENDAR_SUBJECT_RE"),
        ("core/alfred/services/email_security_service.py", "INJECTION_PATTERNS"),
        ("core/alfred/services/inbox_triage_service.py", "ACTION_REQUIRED_PATTERNS"),
        ("core/alfred/services/inbox_triage_service.py", "COMPLEX_STUDY_PATTERNS"),
        ("core/alfred/services/inbox_triage_service.py", "DRIVE_SEARCH_STOPWORDS"),
        ("core/alfred/services/knowledge_service.py", "QUERY_STOPWORDS"),
        ("core/alfred/domain/drive_watcher_models.py", "DEFAULT_EXCLUDED_DRIVE_PATH_PREFIXES"),
        ("core/alfred/services/attachment_extractor.py", "ATTACHMENT_BODY_MARKERS"),
    ]
    for rel_path, sym_name in expected_pairs:
        assert (rel_path, sym_name) in BILINGUAL_RUNTIME_STRING_ALLOWLIST
        allowed_src = f'"""English module."""\n\n{sym_name} = ("Pièce jointe", "échéance")\n'
        assert inspect_python_source(allowed_src, rel_path) == []

    unallowlisted_in_same_file = (
        '"""English module."""\n\n'
        'UI_BANNER = "Résumé des échéances pour l\'utilisateur"\n'
    )
    assert inspect_python_source(
        unallowlisted_in_same_file, "core/alfred/services/inbox_booking_detector.py"
    )
    unallowlisted_in_other_file = (
        '"""English module."""\n\n'
        'PASSIVE_PORTAL_PATTERNS = ("Pièce jointe", "échéance")\n'
    )
    assert inspect_python_source(
        unallowlisted_in_other_file, "core/alfred/services/other_service.py"
    )


def test_inspect_python_source_test_file_mode_allows_fixtures_only() -> None:
    """Verify is_test_file=True permits French runtime fixtures but rejects French docs/comments."""
    from alfred.services.english_policy_service import inspect_python_source

    clean_test_with_fr_fixture = (
        '"""English test module."""\n\n'
        "def test_bilingual_fixture() -> None:\n"
        '    """Verify French runtime fixture handling."""\n'
        '    subject = "Confirmation de réservation à l\'hôtel"\n'
        '    assert "Confirmation" in subject\n'
    )
    assert (
        inspect_python_source(
            clean_test_with_fr_fixture, "core/tests/test_sample.py", is_test_file=True
        )
        == []
    )

    bad_test_doc = (
        '"""Module de test en français."""\n\n'
        "def test_demo() -> None:\n"
        '    """English docstring."""\n'
        "    assert True\n"
    )
    assert inspect_python_source(bad_test_doc, "core/tests/test_sample.py", is_test_file=True)

    bad_test_comment = (
        '"""English test module."""\n\n'
        "def test_demo() -> None:\n"
        '    """English docstring."""\n'
        "    # Verification des regles de classement\n"
        "    assert True\n"
    )
    assert inspect_python_source(bad_test_comment, "core/tests/test_sample.py", is_test_file=True)


def test_inspect_repository_english_detects_docs_and_source_violations(tmp_path: Path) -> None:
    """Verify inspect_repository_english scans repo docs and Python sources accurately."""
    from alfred.services.english_policy_service import inspect_repository_english

    (tmp_path / "docs").mkdir(parents=True)
    (tmp_path / "core" / "alfred" / "services").mkdir(parents=True)
    (tmp_path / "core" / "tests").mkdir(parents=True)

    readme = tmp_path / "README.md"
    agents = tmp_path / "AGENTS.md"
    spec = tmp_path / "docs" / "SPEC.md"
    svc = tmp_path / "core" / "alfred" / "services" / "clean.py"
    tst = tmp_path / "core" / "tests" / "test_clean.py"

    readme.write_text("# Alfred\n\nClean English documentation.\n", encoding="utf-8")
    agents.write_text("# Agents\n\nClean English governance.\n", encoding="utf-8")
    spec.write_text("# Spec\n\nClean English architecture spec.\n", encoding="utf-8")
    svc.write_text('"""Clean English service."""\n\nVAL = "ok"\n', encoding="utf-8")
    tst.write_text(
        '"""English test."""\n\ndef test_ok() -> None:\n    """English."""\n'
        '    sample = "Bonjour l\'utilisateur"\n    assert sample\n',
        encoding="utf-8",
    )
    assert inspect_repository_english(tmp_path) == []

    readme.write_text("# Alfred\n\nRésumé de configuration.\n", encoding="utf-8")
    assert inspect_repository_english(tmp_path)
    readme.write_text("# Alfred\n\nClean English documentation.\n", encoding="utf-8")

    spec.write_text("# Spec\n\nVerification des regles de classement.\n", encoding="utf-8")
    assert inspect_repository_english(tmp_path)
    spec.write_text("# Spec\n\nClean English architecture spec.\n", encoding="utf-8")

    svc.write_text('"""Clean English service."""\n\n# Commentaire français\n', encoding="utf-8")
    assert inspect_repository_english(tmp_path)


def test_evolve_governance_validate_pr_metadata_english() -> None:
    """Verify validate_pr_metadata_english accepts English and rejects FR in title or body."""
    from alfred.services.evolve_governance_service import EvolveGovernanceService

    gov = EvolveGovernanceService()
    assert (
        gov.validate_pr_metadata_english(
            "[BREAKING] feat: enforce English repository policy",
            "## Summary\n- Add deterministic English-only checks -> all gates green.",
        )
        is None
    )

    bad_pairs = [
        ("feat: ajout suivi des échéances", "Clean English body."),
        ("feat: clean English title", "Résumé des changements pour l'utilisateur."),
        ("Verification des regles pour les fichiers", "Clean English body."),
        ("feat: clean English title", "Arbitrage mainteneur requis pour cette PR."),
    ]
    for title, body in bad_pairs:
        with pytest.raises(ValueError):
            gov.validate_pr_metadata_english(title, body)


def test_split_attachment_body_handles_english_and_legacy_markers() -> None:
    """Verify split_attachment_body splits cleanly on new English and legacy French markers."""
    from alfred.services.attachment_extractor import (
        ATTACHMENT_BODY_MARKERS,
        split_attachment_body,
    )

    assert ATTACHMENT_BODY_MARKERS

    assert split_attachment_body("") == ("", "", "")
    assert split_attachment_body("Plain body only") == ("Plain body only", "", "")

    body_en = "Hello Alice\n\n[Attachment: quote.pdf]\nTotal 120 EUR"
    email_en, marker_en, att_en = split_attachment_body(body_en)
    assert email_en == "Hello Alice"
    assert "[Attachment:" in marker_en
    assert "quote.pdf" in att_en

    body_fr = "Bonjour\n\n[Pièce jointe: devis.pdf]\nMontant 120 EUR"
    email_fr, marker_fr, att_fr = split_attachment_body(body_fr)
    assert email_fr == "Bonjour"
    assert "[Pièce jointe:" in marker_fr
    assert "devis.pdf" in att_fr

    lead_en, lead_m_en, lead_a_en = split_attachment_body("[Attachment: solo.txt]\nLine 1")
    assert lead_en == ""
    assert "[Attachment:" in lead_m_en
    assert "solo.txt" in lead_a_en

    lead_fr, lead_m_fr, lead_a_fr = split_attachment_body("[Pièce jointe: solo.txt]\nLigne 1")
    assert lead_fr == ""
    assert "[Pièce jointe:" in lead_m_fr
    assert "solo.txt" in lead_a_fr
