"""Single source of truth for every runtime knob in JobSeekr.

Nothing else in the codebase may hardcode a model name, a path or a credential:
one process, one user, one settings object. Values come from ``.env`` (see
``.env.example``); the defaults here are what a fresh machine runs with.
"""

from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, read once from the environment and cached.

    Field names map to upper-case env keys (``data_dir`` -> ``DATA_DIR``).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ------------------------------------------------------------------ core
    app_env: str = "local"
    log_level: str = "INFO"
    # Display/scheduling timezone only. The DB stores UTC (Claude.md).
    timezone: str = "Australia/Adelaide"
    data_dir: Path = Path("data")
    database_url: str = "sqlite:///data/app.db"
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    frontend_origin: str = "http://localhost:5173"

    # ---------------------------------------------------------------- safety
    # MASTER SWITCH for real submissions. Defaults false and stays false:
    # only the human user ever flips this, and only in their own .env.
    allow_live_submit: bool = False

    # ------------------------------------------------------------------- llm
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    gemini_api_key: str | None = None

    # LiteLLM-style "<provider>/<model>" identifiers. These are the ONLY place
    # a model name may appear; every call site reads them from here.
    llm_model_scoring: str = "anthropic/claude-opus-5"
    llm_model_writing: str = "anthropic/claude-opus-5"
    llm_model_classify: str = "anthropic/claude-opus-5"
    llm_model_formmap: str = "anthropic/claude-opus-5"
    llm_model_embedding: str = "openai/text-embedding-3-small"

    llm_monthly_cap_usd: float = 25.0
    # Warn (Telegram) once spend crosses this fraction of the cap.
    llm_warn_fraction: float = 0.8
    llm_max_retries: int = 3
    llm_timeout_seconds: int = 90

    # --------------------------------------------------------------- browser
    browser_channel: str = "chrome"
    # Defaults to ``data_dir / "browser_profile"`` — filled in by the validator
    # below so it follows a relocated DATA_DIR. Holds a live LinkedIn session;
    # the web UI must never serve files from it.
    browser_profile_dir: Path | None = None
    # Headful is deliberate: headless is a detection signal (Claude.md).
    browser_headless: bool = False

    # ----------------------------------------------------------------- apply
    apply_window_start: str = "09:00"
    apply_window_end: str = "17:00"
    apply_min_interval_floor_seconds: int = 90
    apply_interval_lognormal_mean_seconds: int = 240
    # Unset means "no warm-up ramp configured yet"; guardrails decide policy.
    apply_warmup_start_date: date | None = None

    # ------------------------------------------------------------- documents
    # MiKTeX pdflatex specifically — see the deliberate decisions in Claude.md.
    pdflatex_path: str = "pdflatex"
    # Two passes so LaTeX resolves its own references.
    latex_passes: int = 2

    # -------------------------------------------------------------- telegram
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    # ----------------------------------------------------------------- gmail
    gmail_auth_method: Literal["imap", "oauth"] = "imap"
    gmail_address: str | None = None
    gmail_app_password: str | None = None
    gmail_oauth_client_secret_file: Path | None = None
    gmail_oauth_token_file: Path | None = None
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587

    @model_validator(mode="before")
    @classmethod
    def _blank_is_unset(cls, data: Any) -> Any:
        """Treat ``KEY=`` in .env as unset.

        Leaving a key blank is the natural way to say "I have no Gmail token
        yet". Without this, blanks either fail validation (dates) or coerce into
        something worse than None — ``Path("")`` is ``Path(".")``, which would
        silently point a credential file at the working directory.
        """
        if not isinstance(data, dict):
            return data
        return {
            key: None if isinstance(value, str) and not value.strip() else value
            for key, value in data.items()
        }

    @model_validator(mode="after")
    def _default_browser_profile_dir(self) -> Settings:
        """Anchor the browser profile under data_dir unless explicitly set."""
        if self.browser_profile_dir is None:
            self.browser_profile_dir = self.data_dir / "browser_profile"
        return self

    # ------------------------------------------------------- derived layout
    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def documents_dir(self) -> Path:
        return self.data_dir / "documents"

    @property
    def screenshots_dir(self) -> Path:
        return self.data_dir / "screenshots"

    @property
    def formmaps_dir(self) -> Path:
        return self.data_dir / "formmaps"

    @property
    def formmaps_platform_dir(self) -> Path:
        """Shared, platform-wide form maps (LinkedIn, Workday, ...)."""
        return self.formmaps_dir / "platform"

    @property
    def formmaps_company_dir(self) -> Path:
        """Company overrides, which win over the platform tier."""
        return self.formmaps_dir / "company"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def har_dir(self) -> Path:
        return self.data_dir / "har"

    @property
    def stop_file(self) -> Path:
        """Kill switch: its presence halts the apply loop between jobs."""
        return self.data_dir / "STOP"

    @property
    def managed_directories(self) -> tuple[Path, ...]:
        """Every directory the app owns — the one list ensure_directories walks."""
        assert self.browser_profile_dir is not None  # set by the validator
        return (
            self.data_dir,
            self.logs_dir,
            self.documents_dir,
            self.screenshots_dir,
            self.formmaps_dir,
            self.formmaps_platform_dir,
            self.formmaps_company_dir,
            self.backups_dir,
            self.har_dir,
            self.browser_profile_dir,
        )

    def ensure_directories(self) -> None:
        """Create the whole data tree. Safe to call on every startup path."""
        for directory in self.managed_directories:
            directory.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor so .env is parsed once per process."""
    return Settings()


settings = get_settings()
