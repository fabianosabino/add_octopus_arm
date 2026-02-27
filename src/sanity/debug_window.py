"""
SimpleClaw v2.1 - Debug Window
================================
Mensagem temporária no Telegram que mostra progresso em tempo real.
Edita a mesma mensagem conforme etapas avançam.
Após conclusão, deleta automaticamente após 5 segundos.
Não polui histórico de conversa.

Uso:
    debug = DebugWindow(bot_app, chat_id)
    await debug.open("Iniciando tarefa...")
    await debug.update("⚙️ Executando SQL...")
    await debug.update("✅ Schema aplicado")
    await debug.close()  # deleta após 5s
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

import structlog

logger = structlog.get_logger()


class DebugWindow:
    """
    Mensagem temporária que funciona como janela de debug.

    - Cria uma mensagem no chat
    - Edita em tempo real conforme etapas avançam
    - Deleta automaticamente após conclusão (5s delay)
    - Se o usuário pedir "debug", salva log em arquivo
    """

    def __init__(self, bot_app, chat_id: int, auto_delete_seconds: int = 5):
        self._app = bot_app
        self._chat_id = chat_id
        self._message_id: Optional[int] = None
        self._lines: list[str] = []
        self._auto_delete = auto_delete_seconds
        self._task_id: Optional[str] = None
        self._start_time: Optional[datetime] = None
        self._closed = False

    async def open(self, title: str = "Executando...", task_id: str = "") -> None:
        """Create the debug window message."""
        if self._closed:
            return

        self._task_id = task_id
        self._start_time = datetime.now(timezone.utc)
        self._lines = [f"⚙️ *{title}*", ""]

        try:
            msg = await self._app.bot.send_message(
                chat_id=self._chat_id,
                text=self._render(),
                parse_mode="Markdown",
            )
            self._message_id = msg.message_id
        except Exception as e:
            logger.warning("debug_window.open_failed", error=str(e))

    async def update(self, line: str) -> None:
        """Add a line and update the message."""
        if self._closed or self._message_id is None:
            return

        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self._lines.append(f"`{timestamp}` {line}")

        try:
            await self._app.bot.edit_message_text(
                chat_id=self._chat_id,
                message_id=self._message_id,
                text=self._render(),
                parse_mode="Markdown",
            )
        except Exception as e:
            # Telegram rejects edit if content unchanged or message deleted
            if "message is not modified" not in str(e).lower():
                logger.debug("debug_window.update_failed", error=str(e))

    async def success(self, summary: str = "") -> None:
        """Mark as successful and schedule deletion."""
        if self._closed or self._message_id is None:
            return

        elapsed = ""
        if self._start_time:
            delta = datetime.now(timezone.utc) - self._start_time
            elapsed = f" ({delta.total_seconds():.1f}s)"

        self._lines.append("")
        self._lines.append(f"✅ *Concluído*{elapsed}")
        if summary:
            self._lines.append(f"_{summary}_")

        try:
            await self._app.bot.edit_message_text(
                chat_id=self._chat_id,
                message_id=self._message_id,
                text=self._render(),
                parse_mode="Markdown",
            )
        except Exception:
            pass

        await self._schedule_delete()

    async def error(self, error_msg: str = "") -> None:
        """Mark as failed and schedule deletion."""
        if self._closed or self._message_id is None:
            return

        self._lines.append("")
        self._lines.append(f"❌ *Falha*: {error_msg[:100]}")

        try:
            await self._app.bot.edit_message_text(
                chat_id=self._chat_id,
                message_id=self._message_id,
                text=self._render(),
                parse_mode="Markdown",
            )
        except Exception:
            pass

        await self._schedule_delete()

    async def close(self) -> None:
        """Close and delete the debug window."""
        await self._schedule_delete()

    async def _schedule_delete(self) -> None:
        """Delete message after delay."""
        if self._closed:
            return
        self._closed = True

        async def _delete():
            await asyncio.sleep(self._auto_delete)
            try:
                await self._app.bot.delete_message(
                    chat_id=self._chat_id,
                    message_id=self._message_id,
                )
            except Exception:
                pass  # Message may already be deleted

        asyncio.create_task(_delete())

    def _render(self) -> str:
        """Render all lines into a single message."""
        return "\n".join(self._lines)

    def get_log(self) -> str:
        """Get full log text (for debug file export)."""
        header = f"Task: {self._task_id or 'unknown'}\nStarted: {self._start_time}\n\n"
        return header + "\n".join(self._lines)
