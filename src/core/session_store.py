"""
SimpleClaw v3.0 - Session Store
==================================
Sessões persistentes em JSONL (append-only).
Cada linha é uma mensagem. Se crashar, perde no máximo uma linha.

Inspirado no OpenClaw: cada sessão = um arquivo.
Cada arquivo = uma conversa. Restart e tudo está lá.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger()

DEFAULT_SESSIONS_DIR = Path("/var/simpleclaw/sessions")


class SessionStore:
    """
    JSONL-based session storage.
    Append-only: each message is one line.
    Crash-safe: lose at most one line.
    """

    def __init__(self, sessions_dir: Optional[Path] = None):
        self._dir = sessions_dir or DEFAULT_SESSIONS_DIR
        self._dir.mkdir(parents=True, exist_ok=True)

    def _session_path(self, user_id: str, session_id: str = "main") -> Path:
        user_dir = self._dir / user_id
        user_dir.mkdir(parents=True, exist_ok=True)
        return user_dir / f"{session_id}.jsonl"

    def append(self, user_id: str, message: dict, session_id: str = "main") -> None:
        """
        Append a message to session transcript.

        Message format (OpenAI-compatible):
            {"role": "user", "content": "..."}
            {"role": "assistant", "content": "..."}
            {"role": "assistant", "content": "...", "tool_calls": [...]}
            {"role": "tool", "tool_call_id": "...", "name": "...", "content": "..."}
        """
        path = self._session_path(user_id, session_id)

        entry = {
            **message,
            "_ts": datetime.now(timezone.utc).isoformat(),
        }

        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except Exception as e:
            logger.error("session.append_failed", user_id=user_id, error=str(e))

    def load(self, user_id: str, session_id: str = "main",
             max_messages: int = 50) -> list[dict]:
        """
        Load recent messages from session.
        Returns OpenAI-compatible message list (without _ts metadata).
        """
        path = self._session_path(user_id, session_id)
        if not path.exists():
            return []

        messages = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                        # Strip internal metadata
                        clean = {k: v for k, v in msg.items() if not k.startswith("_")}
                        messages.append(clean)
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            logger.error("session.load_failed", user_id=user_id, error=str(e))
            return []

        # Return last N messages
        if len(messages) > max_messages:
            return messages[-max_messages:]
        return messages

    def get_message_count(self, user_id: str, session_id: str = "main") -> int:
        """Count messages in a session."""
        path = self._session_path(user_id, session_id)
        if not path.exists():
            return 0
        try:
            with open(path, "r") as f:
                return sum(1 for line in f if line.strip())
        except Exception:
            return 0

    def clear(self, user_id: str, session_id: str = "main") -> None:
        """Clear a session (start fresh)."""
        path = self._session_path(user_id, session_id)
        if path.exists():
            path.unlink()
            logger.info("session.cleared", user_id=user_id, session_id=session_id)

    def list_sessions(self, user_id: str) -> list[str]:
        """List all sessions for a user."""
        user_dir = self._dir / user_id
        if not user_dir.exists():
            return []
        return [p.stem for p in user_dir.glob("*.jsonl")]
