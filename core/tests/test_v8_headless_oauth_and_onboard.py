"""TDD specs for V8 headless OAuth2, SSH fallback, V7->V8 scope alignment, and onboarding."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import google_auth_oauthlib.flow as gflow
import pytest
import yaml

from alfred.domain.feature_models import FeatureFlagsConfig
from alfred.domain.models import AlfredPaths, AlfredSettings
from alfred.main import main
from alfred.repositories.state_store import StateStore
from alfred.services.agy_session_manager import AgySessionManager
from alfred.services.drive_service import DriveService
from alfred.services.gmail_service import GmailService
from alfred.services.google_auth import GoogleOAuthService
from alfred.services.inbox_triage_service import InboxTriageService
from alfred.services.knowledge_service import KnowledgeService
from alfred.services.routine_service import RoutineService
from scripts.security_pre_tool_hook import evaluate_pre_tool_event

_AUTH = "https://www.googleapis.com/auth/"


class _FakeFlow:
    """In-memory test double mimicking InstalledAppFlow PKCE and redirect behavior."""

    def __init__(self, creds: Any, **kwargs: Any) -> None:
        """Initialize fake InstalledAppFlow state.

        Args:
            creds: Credentials object returned on token exchange.
            **kwargs: Optional redirect_uri and code_verifier overrides.
        """
        self.credentials = creds
        self.redirect_uri: str | None = kwargs.get("redirect_uri")
        self.code_verifier: str | None = kwargs.get("code_verifier")
        self.last_state = ""
        self.fetched_codes: list[str] = []
        self.local_server_calls: list[dict[str, Any]] = []

    def authorization_url(self, **kwargs: Any) -> tuple[str, str]:
        """Generate an authorization URL and synthesize PKCE code_verifier if unset.

        Args:
            **kwargs: Optional authorization URL parameters.

        Returns:
            Tuple of Google authorization URL and anti-CSRF state token.
        """
        if self.code_verifier is None:
            self.code_verifier = "pkce_verifier_test"
        state = str(kwargs.get("state") or "state_pkce_test")
        self.last_state = state
        return (f"https://accounts.google.com/o/oauth2/auth?state={state}", state)

    def fetch_token(self, **kwargs: Any) -> None:
        """Record authorization code exchange parameters.

        Args:
            **kwargs: Token exchange arguments including authorization code.
        """
        self.fetched_codes.append(str(kwargs.get("code", "")))

    def run_local_server(self, **kwargs: Any) -> Any:
        """Record local HTTP callback server arguments and return fake credentials.

        Args:
            **kwargs: Keyword options passed to run_local_server.

        Returns:
            Fake credentials instance.
        """
        self.local_server_calls.append(dict(kwargs))
        return self.credentials


def _setup_oauth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: str = '{"token": "ok_headless"}',
) -> tuple[AlfredPaths, AlfredSettings, list[_FakeFlow]]:
    """Prepare isolated secrets, load settings, and patch InstalledAppFlow factory.

    Args:
        tmp_path: Pytest temporary directory root.
        monkeypatch: Active pytest monkeypatch fixture.
        payload: JSON credentials string returned by fake credentials.

    Returns:
        Tuple of resolved AlfredPaths, loaded AlfredSettings, and captured flows list.
    """
    paths = AlfredPaths.from_env(tmp_path)
    paths.secrets_dir.mkdir(parents=True, exist_ok=True)
    (paths.secrets_dir / "client_secrets.json").write_text(
        '{"installed": {"client_id": "cid", "client_secret": "fake_secret"}}', encoding="utf-8"
    )
    created: list[_FakeFlow] = []
    creds = MagicMock()
    creds.to_json.return_value = payload

    def _from_secrets(
        _cls: type[Any], /, _secrets: str | Path, _scopes: object, **kwargs: Any
    ) -> _FakeFlow:
        """Construct a fresh _FakeFlow and append it to the capture list.

        Args:
            _cls: InstalledAppFlow class reference.
            _secrets: Path to client_secrets.json.
            _scopes: Requested OAuth2 scope list.
            **kwargs: Optional redirect_uri or code_verifier keyword arguments.

        Returns:
            Newly created _FakeFlow instance.
        """
        flow = _FakeFlow(creds, **kwargs)
        created.append(flow)
        return flow

    monkeypatch.setattr(
        gflow.InstalledAppFlow, "from_client_secrets_file", classmethod(_from_secrets)
    )
    return paths, AlfredSettings.load(paths), created


def test_headless_oauth_start_csrf_complete_and_raw_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify two-step PKCE headless auth, CSRF guard, redirect URL, and raw code.

    Args:
        tmp_path: Pytest temporary directory root.
        monkeypatch: Active pytest monkeypatch fixture.
    """
    paths, settings, flows = _setup_oauth(tmp_path, monkeypatch)
    oauth = GoogleOAuthService(paths, settings)

    auth_url = oauth.start_headless_auth(port=8085)
    assert auth_url.startswith("https://accounts.google.com/")
    assert flows[-1].redirect_uri == "http://localhost:8085/"
    assert flows[-1].code_verifier == "pkce_verifier_test"
    pending_file = paths.secrets_dir / "oauth_pending.json"
    assert pending_file.is_file() and (pending_file.stat().st_mode & 0o777) == 0o600
    pending = json.loads(pending_file.read_text(encoding="utf-8"))
    assert pending["state"] == flows[-1].last_state
    assert pending["code_verifier"] == "pkce_verifier_test"
    assert pending["redirect_uri"] == "http://localhost:8085/"
    assert pending["scopes"] == settings.oauth_scopes

    with pytest.raises(ValueError):
        oauth.complete_headless_auth("http://localhost:8085/?state=wrong_state&code=4/0A_test")
    assert pending_file.is_file()

    state = str(pending["state"])
    tok = oauth.complete_headless_auth(
        f"http://localhost:8085/?state={state}&code=4/0A_test_code&scope=email"
    )
    assert tok == paths.secrets_dir / "token.json"
    assert flows[-1].code_verifier == "pkce_verifier_test"
    assert flows[-1].redirect_uri == "http://localhost:8085/"
    assert flows[-1].fetched_codes == ["4/0A_test_code"]
    assert tok.is_file() and (tok.stat().st_mode & 0o777) == 0o600
    assert tok.read_text(encoding="utf-8") == '{"token": "ok_headless"}'
    assert not pending_file.exists()

    oauth.start_headless_auth(port=8085)
    tok_raw = oauth.complete_headless_auth("4/0A_raw_code_only")
    assert flows[-1].code_verifier == "pkce_verifier_test"
    assert flows[-1].redirect_uri == "http://localhost:8085/"
    assert flows[-1].fetched_codes == ["4/0A_raw_code_only"]
    assert tok_raw.is_file() and not pending_file.exists()


def test_bootstrap_interactive_token_ssh_and_legacy_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify SSH interactive prompt via input_fn and non-TTY local server fallback.

    Args:
        tmp_path: Pytest temporary directory root.
        monkeypatch: Active pytest monkeypatch fixture.
        capsys: Pytest stdout/stderr capture fixture.
    """
    paths, settings, flows = _setup_oauth(tmp_path, monkeypatch, payload='{"token": "ok_ssh"}')

    def _fake_input(_prompt: str) -> str:
        """Synthesize a localhost redirect URL containing the active OAuth state.

        Args:
            _prompt: Interactive prompt string printed for the operator.

        Returns:
            Redirected localhost URL with matching state and authorization code.
        """
        pending_file = paths.secrets_dir / "oauth_pending.json"
        state = (
            str(json.loads(pending_file.read_text(encoding="utf-8"))["state"])
            if pending_file.is_file()
            else flows[-1].last_state
        )
        return f"http://localhost:8085/?state={state}&code=4/0A_ssh_code"

    oauth_ssh = GoogleOAuthService(paths, settings, input_fn=_fake_input)
    tok = oauth_ssh.bootstrap_interactive_token(port=8085, open_browser=False)
    out = capsys.readouterr().out
    assert "https://accounts.google.com/" in out and "http://localhost" in out
    assert flows[-1].local_server_calls == [] and flows[-1].fetched_codes == ["4/0A_ssh_code"]
    assert tok == paths.secrets_dir / "token.json" and tok.is_file()
    assert (tok.stat().st_mode & 0o777) == 0o600

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    oauth_legacy = GoogleOAuthService(paths, settings, input_fn=None)
    tok_legacy = oauth_legacy.bootstrap_interactive_token(port=0, open_browser=False)
    assert len(flows[-1].local_server_calls) == 1
    call = flows[-1].local_server_calls[0]
    assert call["open_browser"] is False and call["port"] == 0
    assert tok_legacy == paths.secrets_dir / "token.json"


def test_v7_to_v8_oauth_scopes_auto_alignment_on_load(tmp_path: Path) -> None:
    """Verify AlfredSettings.load aligns stale V7 google.oauth_scopes to feature flags.

    Args:
        tmp_path: Pytest temporary directory root.
    """
    paths = AlfredPaths.from_env(tmp_path)
    paths.settings_path.parent.mkdir(parents=True, exist_ok=True)
    legacy = [f"{_AUTH}{s}" for s in ("drive", "gmail.modify", "gmail.compose", "calendar")]
    paths.settings_path.write_text(
        yaml.safe_dump({"google": {"oauth_scopes": legacy}}, sort_keys=False), encoding="utf-8"
    )
    loaded = AlfredSettings.load(paths)
    assert loaded.oauth_scopes == loaded.features.compute_oauth_scopes()
    assert len(loaded.oauth_scopes) == 8
    for suffix in ("gmail.settings.basic", "tasks", "contacts", "contacts.other.readonly"):
        assert f"{_AUTH}{suffix}" in loaded.oauth_scopes

    docs_cfg = {
        "google": {"oauth_scopes": legacy},
        "features": {"mcp_gemini_bridge": True, "google_docs_suite": True},
    }
    paths.settings_path.write_text(yaml.safe_dump(docs_cfg, sort_keys=False), encoding="utf-8")
    with_docs = AlfredSettings.load(paths)
    assert with_docs.features.mcp_gemini_bridge and with_docs.features.google_docs_suite
    assert with_docs.oauth_scopes == with_docs.features.compute_oauth_scopes()
    assert f"{_AUTH}documents" in with_docs.oauth_scopes


def test_cli_auth_google_and_apply_onboarding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify auth-google --print-url/--redirect-url and apply-onboarding CLI subcommands.

    Args:
        tmp_path: Pytest temporary directory root.
        monkeypatch: Active pytest monkeypatch fixture.
        capsys: Pytest stdout/stderr capture fixture.
    """
    paths, _, _ = _setup_oauth(tmp_path, monkeypatch, payload='{"token": "ok_cli"}')
    monkeypatch.setenv("ALFRED_ROOT_DIR", str(tmp_path))

    assert main(["auth-google", "--print-url", "--port", "8085"]) == 0
    assert "https://accounts.google.com/" in capsys.readouterr().out
    pending_file = paths.secrets_dir / "oauth_pending.json"
    assert pending_file.is_file()
    state = str(json.loads(pending_file.read_text(encoding="utf-8"))["state"])
    redir_url = f"http://localhost:8085/?state={state}&code=4/0A_cli"
    assert main(["auth-google", "--redirect-url", redir_url]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "OK"
    assert (paths.secrets_dir / "token.json").is_file()

    paths.settings_path.parent.mkdir(parents=True, exist_ok=True)
    cfg = yaml.safe_dump({"google": {"referentiel_drive_folder_name": "00 - Alfred"}})
    paths.settings_path.write_text(cfg, encoding="utf-8")
    paths.knowledge_dir.mkdir(parents=True, exist_ok=True)
    kfile = paths.knowledge_dir / "04_patrimoine.md"
    original = b"---\nid: patrimoine\ntitle: Patrimoine\ndomain: finance\nsummary: Test\n---\n"
    kfile.write_bytes(original)

    rc = main([
        "apply-onboarding", "--enable-all", "--default-send-as",
        "contact@domaine-perso.example", "--never-invite", "contact@domaine-perso.example",
    ])
    _ = capsys.readouterr()
    assert rc == 0
    reloaded = AlfredSettings.load(paths)
    assert len(reloaded.features.enabled_feature_names()) == 10
    assert all(bool(v) for v in reloaded.features.model_dump().values())
    assert reloaded.referentiel_drive_folder_name == "00 - Alfred"
    assert reloaded.identity.default_send_as_email == "contact@domaine-perso.example"
    assert "contact@domaine-perso.example" in reloaded.identity.never_invite_emails
    assert kfile.read_bytes() == original


def test_security_pre_tool_hook_denies_oauth_pending() -> None:
    """Verify security_pre_tool_hook denies direct read_file access to oauth_pending.json."""
    secret = "/root/Alfred/instance/secrets/oauth_pending.json"
    decision = evaluate_pre_tool_event({"tool_name": "read_file", "tool_input": {"Path": secret}})
    assert decision["decision"] == "deny"


def _build_routine_service(
    root: Path, settings: AlfredSettings) -> tuple[RoutineService, list[list[str]]]:
    """Build a RoutineService wired to mocked Google clients and a fake systemctl runner.

    Args:
        root: Isolated ALFRED_ROOT_DIR path.
        settings: Validated AlfredSettings instance.

    Returns:
        Tuple of configured RoutineService and list of captured systemctl commands.
    """
    cmds: list[list[str]] = []

    def _runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """Capture systemctl arguments and return an active CompletedProcess.

        Args:
            cmd: Argument list passed to systemctl.

        Returns:
            CompletedProcess with returncode 0 and active stdout.
        """
        cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "active\n", "")

    paths = AlfredPaths.from_env(root)
    store = StateStore(paths)
    drive = DriveService(paths, settings, store, drive_client=MagicMock())
    gmail = GmailService(paths, settings, gmail_client=MagicMock())
    agy = AgySessionManager(paths, settings, store)
    knowledge = KnowledgeService(paths, settings, store)
    inbox = InboxTriageService(store, gmail, knowledge, drive, agy, settings=settings)
    svc = RoutineService(paths, settings, store, inbox, drive, agy, systemctl_runner=_runner)
    return svc, cmds


def test_routine_install_manages_mcp_service_and_prunes_legacy_v7_timer(tmp_path: Path) -> None:
    """Verify systemd install prunes legacy V7 units and manages alfred-mcp.service by flag.

    Args:
        tmp_path: Pytest temporary directory root.
    """
    unit_dir = tmp_path / "systemd_user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    stale = ("alfred-routines.service", "alfred-routines.timer", "alfred-mcp.service")
    for name in stale:
        (unit_dir / name).write_text("[Unit]\n", encoding="utf-8")

    routines, cmds = _build_routine_service(tmp_path / "no_mcp", AlfredSettings())
    assert len(routines.install_systemd_timers(systemd_user_dir=unit_dir, enable_now=True)) == 4
    for u in ("alfred-inbox", "alfred-periodic"):
        assert (unit_dir / f"{u}.service").is_file() and (unit_dir / f"{u}.timer").is_file()
    assert all(not (unit_dir / name).exists() for name in stale)
    assert ["systemctl", "--user", "disable", "--now", "alfred-routines.timer"] in cmds
    assert ["systemctl", "--user", "disable", "--now", "alfred-mcp.service"] in cmds

    mcp_cfg = AlfredSettings(features=FeatureFlagsConfig(mcp_gemini_bridge=True))
    mcp_routines, mc = _build_routine_service(tmp_path / "with_mcp", mcp_cfg)
    unit_mcp = tmp_path / "systemd_mcp"
    written_mcp = mcp_routines.install_systemd_timers(systemd_user_dir=unit_mcp, enable_now=True)
    assert len(written_mcp) == 5 and (unit_mcp / "alfred-mcp.service").is_file()
    unit_text = (unit_mcp / "alfred-mcp.service").read_text(encoding="utf-8")
    assert "serve-mcp" in unit_text and "Restart=always" in unit_text
    assert "WantedBy=default.target" in unit_text
    svc = "alfred-mcp.service"
    assert any(svc in c and "enable" in c for c in mc)
    assert any(svc in c and any(op in c for op in ("restart", "start", "--now")) for c in mc)


def test_cli_apply_onboarding_mcp_host_and_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Verify apply-onboarding persists --mcp-host, --mcp-port, and --mcp-public-url flags.

    Args:
        tmp_path: Pytest temporary directory root.
        monkeypatch: Active pytest monkeypatch fixture.
        capsys: Pytest stdout/stderr capture fixture.
    """
    paths = AlfredPaths.from_env(tmp_path)
    paths.settings_path.parent.mkdir(parents=True, exist_ok=True)
    cfg = {"identity": {"default_send_as_email": "contact@domaine-perso.example"},
           "features": {"mcp_gemini_bridge": True}, "mcp": {"port": 9000}}
    paths.settings_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    monkeypatch.setenv("ALFRED_ROOT_DIR", str(tmp_path))
    rc = main(["apply-onboarding", "--mcp-host", "0.0.0.0", "--mcp-port", "8765",
               "--mcp-public-url", "https://mcp.domaine-perso.example/"])
    assert rc == 0 and isinstance(json.loads(capsys.readouterr().out), dict)
    reloaded = AlfredSettings.load(paths)
    assert (reloaded.mcp.host, reloaded.mcp.port) == ("0.0.0.0", 8765)
    assert reloaded.mcp.public_url == "https://mcp.domaine-perso.example"
    assert reloaded.identity.default_send_as_email == "contact@domaine-perso.example"
