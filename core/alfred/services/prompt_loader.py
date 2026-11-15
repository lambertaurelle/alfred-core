"""Markdown prompt template loader with instance override and safe placeholder rendering."""

from __future__ import annotations

from pathlib import Path

from alfred.domain.models import AlfredPaths


class PromptTemplateLoader:
    """Load versioned Markdown prompt templates with optional gitignored instance overrides."""

    def __init__(self, paths: AlfredPaths) -> None:
        """Initialize the PromptTemplateLoader.

        Args:
            paths: Resolved AlfredPaths filesystem layout.
        """
        self._paths = paths

    def load_template(self, template_name: str) -> str:
        """Load a Markdown template from instance/config/prompts/ or core/templates/prompts/.

        Args:
            template_name: File name such as 'inbox_triage.md'.

        Returns:
            Raw UTF-8 Markdown template string.
        """
        inst_override = self._paths.instance_dir / "config" / "prompts" / template_name
        if inst_override.exists():
            return inst_override.read_text(encoding="utf-8")
        core_path = self._paths.templates_prompts_dir / template_name
        if core_path.exists():
            return core_path.read_text(encoding="utf-8")
        pkg_fallback = Path(__file__).resolve().parents[2] / "templates" / "prompts" / template_name
        if pkg_fallback.exists():
            return pkg_fallback.read_text(encoding="utf-8")
        raise FileNotFoundError(f"Prompt template '{template_name}' not found in {core_path}")

    def load_custom_inbox_rules(self) -> str:
        """Return optional user-specific inbox rules from instance/config/prompts/ if present.

        Returns:
            Trimmed Markdown rules string, or empty string when not configured.
        """
        custom_path = self._paths.instance_dir / "config" / "prompts" / "inbox_custom_rules.md"
        if not custom_path.exists():
            return ""
        return custom_path.read_text(encoding="utf-8").strip()

    def render(self, template_name: str, context: dict[str, str]) -> str:
        """Render a Markdown template using safe '{{key}}' token replacement.

        Args:
            template_name: File name of the template to load.
            context: Mapping of placeholder names to string values.

        Returns:
            Rendered prompt string preserving any literal single-brace JSON blocks.
        """
        rendered = self.load_template(template_name)
        for key, value in context.items():
            rendered = rendered.replace(f"{{{{{key}}}}}", value)
        return rendered
