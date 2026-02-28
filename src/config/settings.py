"""
SimpleClaw v3.0 - Configuration
================================
Whitelabel settings. Provider-agnostic. Zero hardcoded URLs.
All sensitive values come from environment or vault.
"""

from __future__ import annotations

import os
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ModelProvider(str, Enum):
    """Supported model providers."""
    OLLAMA = "ollama"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GROQ = "groq"
    LITELLM = "litellm"


class LogLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


# ─── BASE PATHS ────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent.parent.parent
CONTEXT_DIR = BASE_DIR / "context"
BACKUP_DIR = BASE_DIR / "backups"
LOG_DIR = BASE_DIR / "logs"
PERSONAS_DIR = Path(__file__).resolve().parent / "personas"


# ─── MAIN SETTINGS ─────────────────────────────────────────

class Settings(BaseSettings):
    """
    SimpleClaw global settings.
    Reads from environment variables with SIMPLECLAW_ prefix.
    """

    model_config = SettingsConfigDict(
        env_prefix="SIMPLECLAW_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Identity ─────────────────────────────
    app_name: str = "SimpleClaw"
    app_version: str = "3.0.0"
    debug: bool = False
    log_level: LogLevel = LogLevel.INFO

    # ── Engine ────────────────────────────────
    engine: str = "loop"  # "loop" (agent loop) ou "agno" (legado)

    # ── Telegram ──────────────────────────────
    telegram_token: str = ""
    telegram_admin_ids: list[int] = Field(default_factory=list)
    telegram_max_concurrent_users: int = 50

    # ── Database ──────────────────────────────
    database_url: str = "postgresql+psycopg://simpleclaw:simpleclaw@localhost:5432/simpleclaw"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    system_schema: str = "system"
    agent_schema: str = "agent"

    # ── Redis ─────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"

    # ── Sessions ──────────────────────────────
    sessions_dir: str = "/var/simpleclaw/sessions"

    # ── Router Model ──────────────────────────
    router_provider: ModelProvider = ModelProvider.GROQ
    router_model_id: str = "llama-3.1-8b-instant"
    router_api_key: Optional[str] = None
    router_api_base: Optional[str] = None  # OBRIGATÓRIO no engine=loop
    router_temperature: float = 0.3
    router_max_tokens: int = 4096

    # ── Specialist Model (legado Agno) ────────
    specialist_provider: ModelProvider = ModelProvider.GROQ
    specialist_model_id: str = "llama-3.1-8b-instant"
    specialist_api_key: Optional[str] = None
    specialist_api_base: Optional[str] = None
    specialist_temperature: float = 0.2
    specialist_max_tokens: int = 8192

    # ── Context ───────────────────────────────
    max_context_tokens: int = 131_000
    compress_threshold_tokens: int = 80_000

    # ── Timeouts ──────────────────────────────
    heartbeat_interval_seconds: int = 240
    heartbeat_timeout_seconds: int = 900
    task_max_retries: int = 3

    # ── Isolation ─────────────────────────────
    worker_base_dir: str = "/tmp/simpleclaw_workers"
    worker_timeout_after_task_minutes: int = 25
    worker_max_concurrent: int = 2

    # ── Search ────────────────────────────────
    searxng_url: str = "http://localhost:8888"
    searxng_timeout_seconds: int = 15

    # ── Vault ─────────────────────────────────
    vault_master_key: str = ""
    vault_rotation_days: int = 90

    # ── Cost Tracking ─────────────────────────
    enable_cost_tracking: bool = True
    cost_alert_threshold_usd: float = 10.0

    # ── Audio / TTS ───────────────────────────
    whisper_provider: str = "groq"
    whisper_api_key: Optional[str] = None
    tts_enabled: bool = False
    tts_voice: str = "pt_BR-faber-medium"

    # ── Superset ──────────────────────────────
    superset_url: str = "http://localhost:8088"
    superset_username: str = "admin"
    superset_password: str = "admin"

    # ── Backup ────────────────────────────────
    backup_cron: str = "0 3 * * *"
    backup_retention_days: int = 30
    history_compress_after_days: int = 30
    history_review_cron: str = "0 10 * * 1"

    # ── Paths ─────────────────────────────────
    context_base_path: str = str(CONTEXT_DIR)
    backup_base_path: str = str(BACKUP_DIR)
    log_path: str = str(LOG_DIR)

    # ── Computed ──────────────────────────────

    def get_router_model_config(self):
        """For legacy Agno compatibility."""
        from src.config.settings import ModelConfig
        return ModelConfig(
            provider=self.router_provider,
            model_id=self.router_model_id,
            api_key=self.router_api_key,
            api_base=self.router_api_base,
            temperature=self.router_temperature,
            max_tokens=self.router_max_tokens,
        )

    @field_validator("vault_master_key")
    @classmethod
    def validate_vault_key(cls, v: str) -> str:
        if not v and os.getenv("SIMPLECLAW_VAULT_MASTER_KEY"):
            return os.getenv("SIMPLECLAW_VAULT_MASTER_KEY", "")
        return v


class ModelConfig(BaseSettings):
    """Configuration for a single model slot."""
    model_config = SettingsConfigDict(extra="ignore")

    provider: ModelProvider = ModelProvider.GROQ
    model_id: str = "llama-3.1-8b-instant"
    api_key: Optional[str] = None
    api_base: Optional[str] = None
    temperature: float = 0.3
    max_tokens: int = 4096
    timeout_seconds: int = 120


# ─── SINGLETON ──────────────────────────────────────────────

_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
