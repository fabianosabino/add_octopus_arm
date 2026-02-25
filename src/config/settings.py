"""
SimpleClaw v2.0 - Configuration
================================
Whitelabel settings with provider-agnostic model configuration.
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


# ─── MODEL CONFIGURATION ───────────────────────────────────

class ModelConfig(BaseSettings):
    """Configuration for a single model slot (router or specialist)."""

    model_config = SettingsConfigDict(extra="ignore")

    provider: ModelProvider = ModelProvider.OLLAMA
    model_id: str = "qwen3:0.6b"
    api_key: Optional[str] = None
    api_base: Optional[str] = None
    temperature: float = 0.7
    max_tokens: int = 4096
    timeout_seconds: int = 120

    def get_agno_model(self):
        """Return the appropriate Agno model instance."""
        if self.provider == ModelProvider.OLLAMA:
            from agno.models.ollama import Ollama
            return Ollama(
                id=self.model_id,
                host=self.api_base or "http://localhost:11434",
                options={"temperature": self.temperature},
            )
        elif self.provider == ModelProvider.OPENAI:
            from agno.models.openai import OpenAIChat
            return OpenAIChat(
                id=self.model_id,
                api_key=self.api_key,
                base_url=self.api_base,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )
        elif self.provider == ModelProvider.ANTHROPIC:
            from agno.models.anthropic import Claude
            return Claude(
                id=self.model_id,
                api_key=self.api_key,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )
        elif self.provider == ModelProvider.GROQ:
            from agno.models.groq import Groq
            return Groq(
                id=self.model_id,
                api_key=self.api_key,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )
        elif self.provider == ModelProvider.LITELLM:
            from agno.models.litellm import LiteLLM
            return LiteLLM(
                id=self.model_id,
                api_key=self.api_key,
                api_base=self.api_base,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )
        else:
            raise ValueError(f"Unsupported provider: {self.provider}")


# ─── MAIN SETTINGS ─────────────────────────────────────────

class Settings(BaseSettings):
    """
    SimpleClaw global settings.
    
    Reads from environment variables with SIMPLECLAW_ prefix.
    Example: SIMPLECLAW_TELEGRAM_TOKEN=xxx
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
    app_version: str = "2.0.0"
    debug: bool = False
    log_level: LogLevel = LogLevel.INFO

    # ── Telegram ─────────────────────────────
    telegram_token: str = ""
    telegram_admin_ids: list[int] = Field(default_factory=list)
    telegram_max_concurrent_users: int = 50

    # ── Database ─────────────────────────────
    database_url: str = "postgresql+psycopg://simpleclaw:simpleclaw@localhost:5432/simpleclaw"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    system_schema: str = "system"
    agent_schema: str = "agent"

    # ── Router Model (always on) ─────────────
    router_provider: ModelProvider = ModelProvider.OLLAMA
    router_model_id: str = "qwen3:0.6b"
    router_api_key: Optional[str] = None
    router_api_base: Optional[str] = None
    router_temperature: float = 0.7
    router_max_tokens: int = 4096

    # ── Specialist Model (on demand) ─────────
    specialist_provider: ModelProvider = ModelProvider.OLLAMA
    specialist_model_id: str = "nanbeige4.1:3b"
    specialist_api_key: Optional[str] = None
    specialist_api_base: Optional[str] = None
    specialist_temperature: float = 0.4
    specialist_max_tokens: int = 8192

    # ── RAM Management ───────────────────────
    max_ram_router_gb: float = 0.8
    max_ram_specialist_gb: float = 2.5
    preload_specialist_on_interaction: bool = True

    # ── Context ──────────────────────────────
    max_context_tokens: int = 131_000
    compress_threshold_tokens: int = 80_000

    # ── Timeouts ─────────────────────────────
    heartbeat_interval_seconds: int = 240
    heartbeat_timeout_seconds: int = 900
    task_max_retries: int = 3
    specialist_preload_timeout_seconds: int = 45

    # ── Isolation (subprocess + venv) ────────
    worker_base_dir: str = "/tmp/simpleclaw_workers"
    worker_timeout_after_task_minutes: int = 25
    worker_max_concurrent: int = 2

    # ── Search ───────────────────────────────
    searxng_url: str = "http://localhost:8888"
    searxng_timeout_seconds: int = 15

    # ── Vault ────────────────────────────────
    vault_master_key: str = ""  # Must be set in env
    vault_rotation_days: int = 90

    # ── Cost Tracking ────────────────────────
    enable_cost_tracking: bool = True
    cost_alert_threshold_usd: float = 10.0

    # ── History ──────────────────────────────
    history_compress_after_days: int = 30
    history_review_cron: str = "0 10 * * 1"  # Every Monday 10am

    # ── Backup ───────────────────────────────
    backup_cron: str = "0 3 * * *"  # Daily 3am
    backup_retention_days: int = 30

    # ── Paths ────────────────────────────────
    context_base_path: str = str(CONTEXT_DIR)
    backup_base_path: str = str(BACKUP_DIR)
    log_path: str = str(LOG_DIR)

    # ── Computed ─────────────────────────────

    def get_router_model_config(self) -> ModelConfig:
        return ModelConfig(
            provider=self.router_provider,
            model_id=self.router_model_id,
            api_key=self.router_api_key,
            api_base=self.router_api_base,
            temperature=self.router_temperature,
            max_tokens=self.router_max_tokens,
        )

    def get_specialist_model_config(self) -> ModelConfig:
        return ModelConfig(
            provider=self.specialist_provider,
            model_id=self.specialist_model_id,
            api_key=self.specialist_api_key,
            api_base=self.specialist_api_base,
            temperature=self.specialist_temperature,
            max_tokens=self.specialist_max_tokens,
        )

    @field_validator("vault_master_key")
    @classmethod
    def validate_vault_key(cls, v: str) -> str:
        if not v and os.getenv("SIMPLECLAW_VAULT_MASTER_KEY"):
            return os.getenv("SIMPLECLAW_VAULT_MASTER_KEY", "")
        return v


# ─── SINGLETON ──────────────────────────────────────────────

_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """Get or create the global settings singleton."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
