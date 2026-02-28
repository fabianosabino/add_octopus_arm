"""
SimpleClaw v3.0 - Telegram Interface
======================================
Multi-tenant bot com debug window, áudio via Whisper/Piper,
e integração com Engine Adapter (agent loop ou Agno via .env).
"""

from __future__ import annotations

import asyncio
import re
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
from src.core.engine_adapter import EngineAdapter
from src.storage.database import get_session
from src.storage.models import User, Task, TaskStatus
from src.tools.cost_tracker import get_user_cost_summary

logger = structlog.get_logger()


class TelegramBot:
    """
    Multi-tenant Telegram bot interface.
    Debug window, audio transcription, and Engine Adapter integration.
    """

    def __init__(self):
        self._settings = get_settings()
        self._engine = EngineAdapter()
        self._app: Optional[Application] = None
        self._active_tasks: dict[int, bool] = {}
        self._watchdog = None

    def set_watchdog(self, watchdog) -> None:
        self._watchdog = watchdog
        if watchdog:
            watchdog.set_notify_callback(self._notify_admin)

    async def start(self) -> None:
        """Initialize and start the Telegram bot."""
        await self._engine.initialize()

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
        self._app.add_handler(CommandHandler("debug", self._cmd_debug))

        # Message handlers
        self._app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, self._handle_audio))
        self._app.add_handler(MessageHandler(filters.PHOTO, self._handle_image))
        self._app.add_handler(MessageHandler(filters.Document.ALL, self._handle_document))
        self._app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text))

        await self._app.bot.set_my_commands([
            BotCommand("start", "Iniciar o SimpleClaw"),
            BotCommand("help", "Ver comandos disponíveis"),
            BotCommand("status", "Status das tarefas"),
            BotCommand("pause", "Pausar tarefa atual"),
            BotCommand("cost", "Ver custos de API"),
            BotCommand("new", "Nova sessão de conversa"),
            BotCommand("health", "Status de saúde do sistema"),
            BotCommand("debug", "Info de debug"),
        ])

        logger.info("telegram.started", engine=self._engine.engine_type)
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)

    async def stop(self) -> None:
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
        await self._engine.shutdown()
        logger.info("telegram.stopped")

    # ─── HELPERS ─────────────────────────────────────────────

    async def _ensure_user(self, update: Update) -> User:
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
        return f"tg_{telegram_id}"

    async def _safe_reply(self, message, text: str) -> None:
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
        if self._app:
            try:
                await self._app.bot.send_message(chat_id=chat_id, text=text)
            except Exception:
                pass

    # ─── COMMANDS ────────────────────────────────────────────

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = await self._ensure_user(update)
        name = user.display_name or user.username or "usuário"
        engine = self._engine.engine_type
        await update.message.reply_text(
            f"👋 Olá, {name}! Sou o *SimpleClaw*, seu assistente pessoal.\n\n"
            "Posso *executar* tarefas reais:\n"
            "• 💬 Conversas e perguntas\n"
            "• 📋 Tarefas técnicas (código, banco de dados, relatórios)\n"
            "• 🔍 Pesquisas na web\n"
            "• 📊 Geração de arquivos (PDF, DOCX, XLSX)\n"
            "• 🎤 Áudio (envie mensagem de voz)\n"
            "• 💰 Controle de custos de API\n\n"
            f"Engine: `{engine}` | Use /help para ver todos os comandos.",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "*Comandos disponíveis:*\n\n"
            "/start — Iniciar o bot\n"
            "/status — Ver status das tarefas\n"
            "/pause — Pausar tarefa em andamento\n"
            "/cost — Ver custos de API do mês\n"
            "/new — Iniciar nova sessão de conversa\n"
            "/debug — Info de debug\n"
            "/help — Esta mensagem\n\n"
            "💡 *Dicas:*\n"
            "• Envie texto para conversar ou solicitar tarefas\n"
            "• Envie áudio para transcrição e resposta por voz\n"
            "• Envie imagens para análise\n"
            "• Envie documentos para processamento\n\n"
            "*O que posso executar:*\n"
            "• `Pesquise preço do bitcoin` → busca real na web\n"
            "• `Crie uma planilha de vendas` → gera XLSX real\n"
            "• `Execute: print('hello')` → roda Python real\n"
            "• `SELECT * FROM users` → executa SQL real",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = await self._ensure_user(update)
        engine = self._engine.engine_type
        session_id = self._get_session_id(user.telegram_id)

        lines = [f"📊 *Status do SimpleClaw*\n"]
        lines.append(f"Engine: `{engine}`")

        # Session info (agent loop)
        if self._engine.session_store:
            count = self._engine.session_store.get_message_count(
                str(user.id), session_id
            )
            lines.append(f"Mensagens na sessão: {count}")

        # Tools info
        try:
            from src.core.tool_registry import build_default_registry
            registry = build_default_registry()
            tools = registry.get_tool_names()
            lines.append(f"Tools ativas: {len(tools)}")
            lines.append(f"Tools: `{', '.join(tools)}`")
        except Exception:
            pass

        # Active tasks from DB
        try:
            async with await get_session() as session:
                from sqlalchemy import select
                stmt = (
                    select(Task)
                    .where(Task.user_id == user.id)
                    .where(Task.status.in_([TaskStatus.PENDING, TaskStatus.PROCESSING, TaskStatus.PAUSED]))
                    .order_by(Task.created_at.desc())
                    .limit(5)
                )
                result = await session.execute(stmt)
                tasks = result.scalars().all()

            if tasks:
                lines.append(f"\n*Tarefas ativas:*")
                for t in tasks:
                    status_emoji = {"pending": "🟡", "processing": "🔵", "paused": "⏸️"}.get(t.status.value, "⚪")
                    lines.append(f"  {status_emoji} {t.title or 'Sem título'}")
        except Exception:
            pass

        await self._safe_reply(update.message, "\n".join(lines))

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
        user = await self._ensure_user(update)
        session_id = self._get_session_id(user.telegram_id)

        # Clear agent loop session if available
        if self._engine.session_store:
            self._engine.session_store.clear(str(user.id), session_id)

        await update.message.reply_text(
            "🔄 Nova sessão iniciada. Contexto anterior mantido na memória."
        )

    async def _cmd_health(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = await self._ensure_user(update)
        if not user.is_admin:
            await update.message.reply_text("⛔ Comando disponível apenas para administradores.")
            return

        lines = ["🏥 *Health Check*\n"]
        lines.append(f"✅ Engine: `{self._engine.engine_type}`")

        # SearXNG
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(f"{self._settings.searxng_url}/healthz")
                lines.append("✅ SearXNG: online")
        except Exception:
            lines.append("❌ SearXNG: offline")

        # PostgreSQL
        try:
            async with await get_session() as session:
                from sqlalchemy import text
                await session.execute(text("SELECT 1"))
                lines.append("✅ PostgreSQL: online")
        except Exception:
            lines.append("❌ PostgreSQL: offline")

        # Watchdog
        if self._watchdog:
            await self._watchdog.check_all()
            report = self._watchdog.get_status_report()
            lines.append(f"\n{report}")
        else:
            lines.append("⚠️ Watchdog: inativo")

        await self._safe_reply(update.message, "\n".join(lines))

    async def _cmd_debug(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show debug information."""
        user = await self._ensure_user(update)
        if not user.is_admin:
            await update.message.reply_text("⛔ Comando disponível apenas para administradores.")
            return

        settings = self._settings
        lines = [
            "🔧 *Debug Info*\n",
            f"Engine: `{self._engine.engine_type}`",
            f"Provider: `{settings.router_provider.value}`",
            f"Model: `{settings.router_model_id}`",
            f"API Base: `{settings.router_api_base or 'not set'}`",
            f"Temperature: `{settings.router_temperature}`",
            f"Max Tokens: `{settings.router_max_tokens}`",
        ]

        # Tool list
        try:
            from src.core.tool_registry import build_default_registry
            registry = build_default_registry()
            names = registry.get_tool_names()
            lines.append(f"\nTools ({len(names)}): `{', '.join(names)}`")
        except Exception:
            pass

        await self._safe_reply(update.message, "\n".join(lines))

    async def _notify_admin(self, telegram_id: int, message: str) -> None:
        if self._app:
            try:
                await self._app.bot.send_message(
                    chat_id=telegram_id, text=message, parse_mode=ParseMode.MARKDOWN,
                )
            except Exception:
                try:
                    await self._app.bot.send_message(
                        chat_id=telegram_id, text=message, parse_mode=None,
                    )
                except Exception:
                    pass

    async def _send_file(self, chat_id: int, filepath: Path, caption: str = "") -> None:
        if not filepath.exists():
            await self._app.bot.send_message(chat_id=chat_id, text="❌ Arquivo não encontrado.")
            return

        with open(filepath, "rb") as f:
            await self._app.bot.send_document(
                chat_id=chat_id, document=f, filename=filepath.name,
                caption=caption or f"📎 {filepath.name}",
            )

    # ─── MESSAGE HANDLERS ────────────────────────────────────

    async def _handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming text messages via Engine Adapter."""
        user = await self._ensure_user(update)
        message = update.message.text
        session_id = self._get_session_id(user.telegram_id)
        user_id = str(user.id)
        chat_id = update.effective_chat.id

        await update.message.chat.send_action(ChatAction.TYPING)

        try:
            response = await self._engine.chat(
                message=message,
                user_id=user_id,
                session_id=session_id,
                chat_id=chat_id,
            )

            await self._safe_reply(update.message, response)
            await self._send_generated_files(chat_id, response)

        except Exception as e:
            logger.error("handle_text.error", error=str(e), user_id=user_id)
            await self._safe_reply(
                update.message,
                "Encontrei um problema ao processar sua mensagem. "
                "Estou me recuperando. Tente novamente em alguns segundos."
            )

    async def _handle_audio(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle voice messages: transcribe with Whisper, process, respond with TTS."""
        user = await self._ensure_user(update)
        session_id = self._get_session_id(user.telegram_id)
        user_id = str(user.id)

        await update.message.chat.send_action(ChatAction.TYPING)

        # Download audio
        if update.message.voice:
            file = await update.message.voice.get_file()
        else:
            file = await update.message.audio.get_file()

        audio_path = Path(f"/tmp/simpleclaw_audio_{user.telegram_id}.ogg")
        await file.download_to_drive(str(audio_path))

        # Transcribe
        try:
            from src.audio.audio_tools import transcribe_audio
            result = await transcribe_audio(audio_path)

            if not result["success"]:
                await self._safe_reply(
                    update.message,
                    f"Não consegui transcrever o áudio: {result['error']}\n"
                    "Por enquanto, envie sua mensagem em texto."
                )
                return

            transcribed_text = result["text"]
            if not transcribed_text.strip():
                await update.message.reply_text("Não detectei fala no áudio. Tente novamente.")
                return

            # Show transcription
            await update.message.reply_text(f"🎤 _{transcribed_text}_", parse_mode=ParseMode.MARKDOWN)

            # Process via engine adapter
            response = await self._engine.chat(
                message=transcribed_text,
                user_id=user_id,
                session_id=session_id,
                chat_id=update.effective_chat.id,
            )
            await self._safe_reply(update.message, response)
            await self._send_generated_files(update.effective_chat.id, response)

            # Try TTS response
            try:
                from src.audio.audio_tools import synthesize_speech, convert_wav_to_ogg
                tts_result = await synthesize_speech(response)

                if tts_result["success"]:
                    ogg_path = await convert_wav_to_ogg(tts_result["audio_path"])
                    if ogg_path and ogg_path.exists():
                        with open(ogg_path, "rb") as audio_file:
                            await update.message.reply_voice(voice=audio_file)
                        ogg_path.unlink(missing_ok=True)
                    if tts_result["audio_path"]:
                        tts_result["audio_path"].unlink(missing_ok=True)
            except ImportError:
                pass  # TTS not available, text response is enough

        except ImportError:
            # Audio tools not installed yet
            await update.message.reply_text(
                "🎤 Áudio recebido, mas o módulo de transcrição ainda não está ativo.\n"
                "Por enquanto, envie sua mensagem em texto."
            )
        except Exception as e:
            logger.error("handle_audio.error", error=str(e))
            await self._safe_reply(
                update.message,
                "Erro ao processar áudio. Tente enviar em texto."
            )
        finally:
            audio_path.unlink(missing_ok=True)

    async def _handle_image(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = await self._ensure_user(update)
        await update.message.reply_text("🖼️ Imagem recebida. Analisando...")

        photo = update.message.photo[-1]
        file = await photo.get_file()

        image_path = Path(f"/tmp/simpleclaw_img_{user.telegram_id}.jpg")
        await file.download_to_drive(str(image_path))

        caption = update.message.caption or "Analise esta imagem."
        session_id = self._get_session_id(user.telegram_id)

        response = await self._engine.chat(
            message=f"[Imagem recebida] {caption}",
            user_id=str(user.id),
            session_id=session_id,
            chat_id=update.effective_chat.id,
        )
        await self._safe_reply(update.message, response)

    async def _handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

        response = await self._engine.chat(
            message=f"[Documento: {doc.file_name}, salvo em {doc_path}] {caption}",
            user_id=str(user.id),
            session_id=session_id,
            chat_id=update.effective_chat.id,
        )
        await self._safe_reply(update.message, response)
        await self._send_generated_files(update.effective_chat.id, response)

    async def _send_generated_files(self, chat_id: int, result_text: str) -> None:
        """Detect file paths in agent output and send them via Telegram."""
        file_patterns = re.findall(r'/tmp/simpleclaw[_\w]*/[\w./\-]+', result_text)
        for filepath_str in file_patterns:
            # Strip trailing punctuation that regex might catch
            filepath_str = filepath_str.rstrip('.,;:!?)')
            filepath = Path(filepath_str)
            if filepath.exists() and filepath.is_file():
                await self._send_file(chat_id, filepath)
