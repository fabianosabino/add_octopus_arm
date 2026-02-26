"""
SimpleClaw v2.0 - Telegram Interface
======================================
Multi-tenant Telegram bot with command handling,
multimedia support, and file delivery.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Optional

import structlog
from telegram import Update, BotCommand
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from src.config.settings import get_settings
from src.agents.router import RouterAgent, Intent
from src.agents.specialist_team import SpecialistManager
from src.agents.task_executor import TaskExecutor
from src.storage.database import get_session
from src.storage.models import User, Task, TaskStatus
from src.tools.cost_tracker import get_user_cost_summary

logger = structlog.get_logger()


class TelegramBot:
    """
    Multi-tenant Telegram bot interface.
    
    Each user gets their own session and memory context.
    Handles text, images, audio, and file delivery.
    """

    def __init__(self):
        self._settings = get_settings()
        self._router = RouterAgent()
        self._specialist = SpecialistManager()
        self._task_executor: Optional[TaskExecutor] = None
        self._app: Optional[Application] = None
        self._active_tasks: dict[int, bool] = {}
        self._watchdog = None

    def set_watchdog(self, watchdog) -> None:
        """Set watchdog reference for health checks."""
        self._watchdog = watchdog
        if watchdog:
            watchdog.set_notify_callback(self._notify_admin)

    async def start(self) -> None:
        """Initialize and start the Telegram bot."""
        await self._router.initialize()

        # Create task executor with resilient loop
        self._task_executor = TaskExecutor(self._specialist, self._router)
        self._task_executor.set_notify_callback(self._send_progress)

        # Wire specialist progress notifications
        self._specialist.set_notify_callback(self._send_progress)

        self._app = (
            Application.builder()
            .token(self._settings.telegram_token)
            .build()
        )

        # Register handlers
        self._app.add_handler(CommandHandler("start", self._cmd_start))
        self._app.add_handler(CommandHandler("help", self._cmd_help))
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("pause", self._cmd_pause))
        self._app.add_handler(CommandHandler("cost", self._cmd_cost))
        self._app.add_handler(CommandHandler("new", self._cmd_new_session))
        self._app.add_handler(CommandHandler("health", self._cmd_health))

        # Message handlers (order matters)
        self._app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, self._handle_audio))
        self._app.add_handler(MessageHandler(filters.PHOTO, self._handle_image))
        self._app.add_handler(MessageHandler(filters.Document.ALL, self._handle_document))
        self._app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text))

        # Set bot commands
        await self._app.bot.set_my_commands([
            BotCommand("start", "Iniciar o SimpleClaw"),
            BotCommand("help", "Ver comandos disponíveis"),
            BotCommand("status", "Status das tarefas"),
            BotCommand("pause", "Pausar tarefa atual"),
            BotCommand("cost", "Ver custos de API"),
            BotCommand("new", "Nova sessão de conversa"),
            BotCommand("health", "Status de saúde do sistema"),
        ])

        logger.info("telegram.started")
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)

    async def stop(self) -> None:
        """Stop the Telegram bot gracefully."""
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
        await self._specialist.shutdown()
        logger.info("telegram.stopped")

    # ─── HELPERS ─────────────────────────────────────────────

    async def _ensure_user(self, update: Update) -> User:
        """Get or create user in database."""
        tg_user = update.effective_user
        async with await get_session() as session:
            from sqlalchemy import select
            stmt = select(User).where(User.telegram_id == tg_user.id)
            result = await session.execute(stmt)
            user = result.scalar_one_or_none()

            if not user:
                user = User(
                    telegram_id=tg_user.id,
                    username=tg_user.username,
                    display_name=tg_user.full_name,
                    is_admin=tg_user.id in self._settings.telegram_admin_ids,
                )
                session.add(user)
                await session.commit()
                await session.refresh(user)
                logger.info("user.created", telegram_id=tg_user.id, name=tg_user.full_name)

            return user

    def _get_session_id(self, telegram_id: int) -> str:
        """Generate a deterministic session ID per user."""
        return f"tg_{telegram_id}"

    async def _safe_reply(self, message, text: str) -> None:
        """Send message with Markdown, fallback to plain text on parse error."""
        try:
            await message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            try:
                await message.reply_text(text, parse_mode=None)
            except Exception:
                await message.reply_text(
                    "Resposta gerada mas houve erro na formatação. Tente novamente."
                )

    async def _send_progress(self, chat_id: int, text: str) -> None:
        """Send progress update to user (used by specialist for step-by-step)."""
        if self._app:
            try:
                await self._app.bot.send_message(chat_id=chat_id, text=text)
            except Exception:
                pass

    # ─── COMMANDS ────────────────────────────────────────────

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = await self._ensure_user(update)
        name = user.display_name or user.username or "usuário"
        await update.message.reply_text(
            f"👋 Olá, {name}! Sou o *SimpleClaw*, seu assistente pessoal.\n\n"
            "Posso te ajudar com:\n"
            "• 💬 Conversas e perguntas\n"
            "• 📋 Tarefas técnicas (código, banco de dados, relatórios)\n"
            "• 🔍 Pesquisas na web\n"
            "• 📊 Geração de arquivos (PDF, DOCX, XLSX)\n"
            "• ⏰ Agendamentos e lembretes\n"
            "• 💰 Controle de custos de API\n\n"
            "Use /help para ver todos os comandos.",
            parse_mode=ParseMode.MARKDOWN,
        )

        if self._settings.preload_specialist_on_interaction:
            asyncio.create_task(self._specialist.preload_model())

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "*Comandos disponíveis:*\n\n"
            "/start — Iniciar o bot\n"
            "/status — Ver status das tarefas\n"
            "/pause — Pausar tarefa em andamento\n"
            "/cost — Ver custos de API do mês\n"
            "/new — Iniciar nova sessão de conversa\n"
            "/help — Esta mensagem\n\n"
            "💡 *Dicas:*\n"
            "• Envie texto para conversar ou solicitar tarefas\n"
            "• Envie áudio para transcrição automática\n"
            "• Envie imagens para análise\n"
            "• Envie documentos para processamento",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = await self._ensure_user(update)

        async with await get_session() as session:
            from sqlalchemy import select
            stmt = (
                select(Task)
                .where(Task.user_id == user.id)
                .where(Task.status.in_([TaskStatus.PENDING, TaskStatus.PROCESSING, TaskStatus.PAUSED]))
                .order_by(Task.created_at.desc())
                .limit(10)
            )
            result = await session.execute(stmt)
            tasks = result.scalars().all()

        tasks_data = [
            {
                "title": t.title,
                "status": t.status,
                "started_at": t.started_at.strftime("%d/%m %H:%M") if t.started_at else None,
            }
            for t in tasks
        ]
        response = await self._router.format_status_response(tasks_data, str(user.id))
        await self._safe_reply(update.message, response)

    async def _cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = await self._ensure_user(update)
        async with await get_session() as session:
            from sqlalchemy import select, update as sql_update
            stmt = (
                sql_update(Task)
                .where(Task.user_id == user.id, Task.status == TaskStatus.PROCESSING)
                .values(status=TaskStatus.PAUSED)
            )
            await session.execute(stmt)
            await session.commit()
        await update.message.reply_text("⏸️ Tarefa pausada. Use /status para verificar.")

    async def _cmd_cost(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = await self._ensure_user(update)
        summary = await get_user_cost_summary(user.id, days=30)

        lines = [f"💰 *Custos dos últimos 30 dias:*\n"]
        lines.append(f"Total: ${summary['total_cost_usd']:.4f} USD")
        lines.append(f"Tokens: {summary['total_tokens']:,}")
        lines.append(f"Requisições: {summary['total_requests']}\n")

        if summary["by_model"]:
            lines.append("*Por modelo:*")
            for m in summary["by_model"]:
                lines.append(f"  • {m['model']}: ${m['cost_usd']:.4f} ({m['tokens']:,} tokens)")

        await self._safe_reply(update.message, "\n".join(lines))

    async def _cmd_new_session(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "🔄 Nova sessão iniciada. Contexto anterior mantido na memória."
        )

    async def _cmd_health(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show system health status (admin only)."""
        user = await self._ensure_user(update)
        if not user.is_admin:
            await update.message.reply_text("⛔ Comando disponível apenas para administradores.")
            return

        if self._watchdog:
            await self._watchdog.check_all()
            report = self._watchdog.get_status_report()
        else:
            report = "⚠️ Watchdog não inicializado."

        await self._safe_reply(update.message, report)

    async def _notify_admin(self, telegram_id: int, message: str) -> None:
        """Send notification to admin (used by watchdog)."""
        if self._app:
            try:
                await self._app.bot.send_message(
                    chat_id=telegram_id,
                    text=message,
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception:
                try:
                    await self._app.bot.send_message(
                        chat_id=telegram_id,
                        text=message,
                        parse_mode=None,
                    )
                except Exception:
                    pass

    async def _send_file(self, chat_id: int, filepath: Path, caption: str = "") -> None:
        """Send a generated file to the user via Telegram."""
        if not filepath.exists():
            await self._app.bot.send_message(chat_id=chat_id, text="❌ Arquivo não encontrado.")
            return

        with open(filepath, "rb") as f:
            await self._app.bot.send_document(
                chat_id=chat_id,
                document=f,
                filename=filepath.name,
                caption=caption or f"📎 {filepath.name}",
            )

    # ─── MESSAGE HANDLERS ────────────────────────────────────

    async def _handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming text messages with full error boundary."""
        user = await self._ensure_user(update)
        message = update.message.text
        session_id = self._get_session_id(user.telegram_id)
        user_id = str(user.id)
        chat_id = update.effective_chat.id

        await update.message.chat.send_action(ChatAction.TYPING)

        try:
            intent = await self._router.classify_intent(message)
            logger.info("message.classified", intent=intent.value, user_id=user_id)

            if intent == Intent.CHAT:
                response = await self._router.chat(message, user_id, session_id)
                await self._safe_reply(update.message, response)

            elif intent == Intent.TASK:
                result = await self._task_executor.execute(
                    request=message,
                    user_id=user_id,
                    session_id=session_id,
                    chat_id=chat_id,
                )
                await self._safe_reply(update.message, result)
                await self._send_generated_files(chat_id, result)

            elif intent == Intent.STATUS:
                await self._cmd_status(update, context)

            elif intent == Intent.SCHEDULE:
                response = await self._router.chat(
                    f"O usuário quer agendar algo: {message}", user_id, session_id
                )
                await self._safe_reply(update.message, response)

            elif intent == Intent.SEARCH:
                await update.message.reply_text("🔍 Pesquisando...")
                response = await self._router.chat(
                    f"Pesquise e responda: {message}", user_id, session_id
                )
                await self._safe_reply(update.message, response)

            else:
                response = await self._router.chat(message, user_id, session_id)
                await self._safe_reply(update.message, response)

        except Exception as e:
            logger.error("handle_text.error", error=str(e), user_id=user_id)
            await self._safe_reply(
                update.message,
                "Encontrei um problema ao processar sua mensagem. "
                "Estou me recuperando. Tente novamente em alguns segundos."
            )

    async def _handle_audio(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle voice messages and audio files."""
        user = await self._ensure_user(update)
        await update.message.reply_text("🎤 Recebendo áudio... Transcrição em breve.")

        if update.message.voice:
            file = await update.message.voice.get_file()
        else:
            file = await update.message.audio.get_file()

        audio_path = Path(f"/tmp/simpleclaw_audio_{user.telegram_id}.ogg")
        await file.download_to_drive(str(audio_path))

        await update.message.reply_text(
            "📝 Áudio recebido. A transcrição será implementada com Whisper. "
            "Por enquanto, envie sua mensagem em texto."
        )

    async def _handle_image(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle image messages."""
        user = await self._ensure_user(update)
        await update.message.reply_text("🖼️ Imagem recebida. Analisando...")

        photo = update.message.photo[-1]
        file = await photo.get_file()

        image_path = Path(f"/tmp/simpleclaw_img_{user.telegram_id}.jpg")
        await file.download_to_drive(str(image_path))

        caption = update.message.caption or "Analise esta imagem."
        session_id = self._get_session_id(user.telegram_id)

        response = await self._router.chat(
            f"[Imagem recebida] {caption}",
            str(user.id),
            session_id,
        )
        await self._safe_reply(update.message, response)

    async def _handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle document uploads."""
        user = await self._ensure_user(update)
        doc = update.message.document
        await self._safe_reply(
            update.message,
            f"📄 Documento recebido: {doc.file_name} ({doc.file_size // 1024}KB). Processando..."
        )

        file = await doc.get_file()
        doc_path = Path(f"/tmp/simpleclaw_doc_{user.telegram_id}_{doc.file_name}")
        await file.download_to_drive(str(doc_path))

        caption = update.message.caption or f"Processar documento: {doc.file_name}"
        session_id = self._get_session_id(user.telegram_id)

        response = await self._router.chat(
            f"[Documento: {doc.file_name}] {caption}",
            str(user.id),
            session_id,
        )
        await self._safe_reply(update.message, response)

    async def _send_generated_files(self, chat_id: int, result_text: str) -> None:
        """Detect file paths in agent output and send them via Telegram."""
        import re
        file_patterns = re.findall(r'/tmp/simpleclaw_files/[^\s\n]+', result_text)
        for filepath_str in file_patterns:
            filepath = Path(filepath_str)
            if filepath.exists() and filepath.is_file():
                await self._send_file(chat_id, filepath)
