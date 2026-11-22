"""Domain schemas for V8 evolution qualification, living SPEC snapshots, and breaking reports."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EvolutionMode(StrEnum):
    """Target execution mode for an Alfred evolution proposal."""

    INSTANCE = "instance"
    UPSTREAM_PR = "upstream-pr"
    LOCAL_FORK = "local-fork"


class EvolutionArchitectureKind(StrEnum):
    """Architectural classification of an evolution request across core/ vs instance/."""

    PURE_INSTANCE = "PURE_INSTANCE"
    INSTANCE_WITH_COMMUNITY_OPTION = "INSTANCE_WITH_COMMUNITY_OPTION"
    HYBRID_CORE_AND_INSTANCE = "HYBRID_CORE_AND_INSTANCE"
    CORE_EVOLUTION = "CORE_EVOLUTION"


class UseCaseQualification(BaseModel):
    """Architectural qualification verdict guiding mode selection and hybrid completeness."""

    model_config = ConfigDict(frozen=True)

    architecture_kind: EvolutionArchitectureKind
    recommended_mode: EvolutionMode
    core_feature_is_standalone_complete: bool = True
    rationale: str = ""
    community_pr_option_summary: str = ""
    rearchitecture_suggestion: str = ""


class CoreModuleEntry(BaseModel):
    """AST-extracted inventory descriptor for one Python module under core/alfred/."""

    model_config = ConfigDict(frozen=True)

    layer: str
    module_path: str
    summary: str = ""


class CliCommandSpec(BaseModel):
    """AST-extracted CLI subcommand specification and option flags."""

    model_config = ConfigDict(frozen=True)

    name: str
    options: list[str] = Field(default_factory=list)
    source_module: str = ""


class WorkspaceSpecEntry(BaseModel):
    """Specialist Antigravity workspace entry extracted from core/workspaces/."""

    model_config = ConfigDict(frozen=True)

    name: str
    summary: str = ""


class FeatureSpecEntry(BaseModel):
    """AST-extracted modular feature flag entry with GCP APIs and OAuth2 scopes."""

    model_config = ConfigDict(frozen=True)

    flag_name: str
    default_enabled: bool = False
    gcp_apis: list[str] = Field(default_factory=list)
    oauth_scopes: list[str] = Field(default_factory=list)


class CoreSpecSnapshot(BaseModel):
    """Deterministic AST snapshot of alfred-core used to render docs/SPEC.md."""

    model_config = ConfigDict(frozen=True)

    version: str
    workspaces: list[WorkspaceSpecEntry] = Field(default_factory=list)
    features: list[FeatureSpecEntry] = Field(default_factory=list)
    modules: list[CoreModuleEntry] = Field(default_factory=list)
    cli_commands: list[CliCommandSpec] = Field(default_factory=list)


class BreakingChangeReport(BaseModel):
    """Automated breaking-change analysis comparing a worktree against the base branch."""

    model_config = ConfigDict(frozen=True)

    is_breaking: bool = False
    removed_modules: list[str] = Field(default_factory=list)
    removed_cli_commands: list[str] = Field(default_factory=list)
    removed_feature_flags: list[str] = Field(default_factory=list)
    summary_markdown: str = ""
