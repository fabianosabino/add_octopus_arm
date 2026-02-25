"""
SimpleClaw v2.0 - Vault
========================
Encrypted credential storage using Fernet symmetric encryption.
Supports per-user keys, rotation tracking, and expiration.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import structlog
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select, and_

from src.config.settings import get_settings
from src.storage.database import get_session
from src.storage.models import VaultEntry

logger = structlog.get_logger()


class Vault:
    """Encrypted key-value store backed by PostgreSQL."""

    def __init__(self, master_key: Optional[str] = None):
        settings = get_settings()
        raw_key = master_key or settings.vault_master_key
        if not raw_key:
            raise ValueError(
                "Vault master key not set. "
                "Set SIMPLECLAW_VAULT_MASTER_KEY environment variable."
            )
        # Derive a 32-byte Fernet key from master key
        derived = hashlib.sha256(raw_key.encode()).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(derived))
        self._rotation_days = settings.vault_rotation_days

    def _encrypt(self, plaintext: str) -> str:
        """Encrypt a string value."""
        return self._fernet.encrypt(plaintext.encode()).decode()

    def _decrypt(self, ciphertext: str) -> str:
        """Decrypt an encrypted string value."""
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken:
            raise ValueError("Failed to decrypt vault entry. Master key may have changed.")

    async def store(
        self,
        key_name: str,
        value: str,
        user_id: Optional[uuid.UUID] = None,
        provider: Optional[str] = None,
        expires_in_days: Optional[int] = None,
    ) -> VaultEntry:
        """Store or update an encrypted credential."""
        encrypted = self._encrypt(value)
        expires_at = None
        if expires_in_days:
            expires_at = datetime.now(timezone.utc) + timedelta(days=expires_in_days)

        async with await get_session() as session:
            async with session.begin():
                # Check for existing entry
                stmt = select(VaultEntry).where(
                    and_(
                        VaultEntry.key_name == key_name,
                        VaultEntry.user_id == user_id,
                    )
                )
                result = await session.execute(stmt)
                entry = result.scalar_one_or_none()

                if entry:
                    entry.encrypted_value = encrypted
                    entry.provider = provider
                    entry.rotated_at = datetime.now(timezone.utc)
                    entry.expires_at = expires_at
                    logger.info("vault.updated", key=key_name, user_id=str(user_id))
                else:
                    entry = VaultEntry(
                        key_name=key_name,
                        encrypted_value=encrypted,
                        user_id=user_id,
                        provider=provider,
                        expires_at=expires_at,
                    )
                    session.add(entry)
                    logger.info("vault.stored", key=key_name, user_id=str(user_id))

                return entry

    async def retrieve(
        self,
        key_name: str,
        user_id: Optional[uuid.UUID] = None,
    ) -> Optional[str]:
        """Retrieve and decrypt a credential."""
        async with await get_session() as session:
            stmt = select(VaultEntry).where(
                and_(
                    VaultEntry.key_name == key_name,
                    VaultEntry.user_id == user_id,
                )
            )
            result = await session.execute(stmt)
            entry = result.scalar_one_or_none()

            if not entry:
                return None

            # Check expiration
            if entry.expires_at and entry.expires_at < datetime.now(timezone.utc):
                logger.warning("vault.expired", key=key_name)
                return None

            return self._decrypt(entry.encrypted_value)

    async def delete(
        self,
        key_name: str,
        user_id: Optional[uuid.UUID] = None,
    ) -> bool:
        """Delete a credential."""
        async with await get_session() as session:
            async with session.begin():
                stmt = select(VaultEntry).where(
                    and_(
                        VaultEntry.key_name == key_name,
                        VaultEntry.user_id == user_id,
                    )
                )
                result = await session.execute(stmt)
                entry = result.scalar_one_or_none()
                if entry:
                    await session.delete(entry)
                    logger.info("vault.deleted", key=key_name)
                    return True
                return False

    async def list_keys(
        self,
        user_id: Optional[uuid.UUID] = None,
    ) -> list[dict]:
        """List all keys (without values) for a user."""
        async with await get_session() as session:
            stmt = select(VaultEntry).where(VaultEntry.user_id == user_id)
            result = await session.execute(stmt)
            entries = result.scalars().all()
            return [
                {
                    "key_name": e.key_name,
                    "provider": e.provider,
                    "rotated_at": e.rotated_at.isoformat(),
                    "expires_at": e.expires_at.isoformat() if e.expires_at else None,
                }
                for e in entries
            ]

    async def check_rotation_needed(self) -> list[dict]:
        """Find credentials that need rotation."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=self._rotation_days)
        async with await get_session() as session:
            stmt = select(VaultEntry).where(VaultEntry.rotated_at < cutoff)
            result = await session.execute(stmt)
            entries = result.scalars().all()
            return [
                {"key_name": e.key_name, "user_id": str(e.user_id), "days_since_rotation": (datetime.now(timezone.utc) - e.rotated_at).days}
                for e in entries
            ]
