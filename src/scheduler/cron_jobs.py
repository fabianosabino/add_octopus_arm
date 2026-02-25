"""
SimpleClaw v2.0 - Scheduler
=============================
Manages system and user-defined scheduled jobs.
Uses APScheduler with PostgreSQL job store for persistence.
Handles: backups, history digests, rotation checks, user crons.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select, and_, func

from src.config.settings import get_settings
from src.storage.database import get_session
from src.storage.models import (
    Conversation,
    HistoryDigest,
    Schedule,
    User,
)

logger = structlog.get_logger()


class SchedulerService:
    """
    Manages all scheduled operations:
    - System: backups, history compression, vault rotation checks
    - User: custom cron jobs defined via chat
    """

    def __init__(self):
        self._settings = get_settings()
        self._scheduler: Optional[AsyncIOScheduler] = None
        self._telegram_bot = None  # Set after bot initialization

    def set_telegram_bot(self, bot) -> None:
        """Set telegram bot reference for sending notifications."""
        self._telegram_bot = bot

    async def start(self) -> None:
        """Initialize and start the scheduler."""
        self._scheduler = AsyncIOScheduler(timezone="America/Sao_Paulo")

        # ── System Jobs ──────────────────────────

        # Daily backup (3am)
        self._scheduler.add_job(
            self._job_daily_backup,
            CronTrigger.from_crontab(self._settings.backup_cron),
            id="system_backup",
            replace_existing=True,
        )

        # Weekly history digest (Monday 10am)
        self._scheduler.add_job(
            self._job_history_digest,
            CronTrigger.from_crontab(self._settings.history_review_cron),
            id="system_history_digest",
            replace_existing=True,
        )

        # Daily vault rotation check (6am)
        self._scheduler.add_job(
            self._job_check_vault_rotation,
            CronTrigger(hour=6, minute=0),
            id="system_vault_check",
            replace_existing=True,
        )

        # Load user-defined schedules from DB
        await self._load_user_schedules()

        self._scheduler.start()
        logger.info("scheduler.started", jobs=len(self._scheduler.get_jobs()))

    async def stop(self) -> None:
        """Shutdown scheduler gracefully."""
        if self._scheduler:
            self._scheduler.shutdown(wait=True)
        logger.info("scheduler.stopped")

    # ─── USER SCHEDULE MANAGEMENT ────────────────────────────

    async def add_user_schedule(
        self,
        user_id,
        name: str,
        cron_expression: str,
        action_type: str,
        action_payload: dict,
        description: Optional[str] = None,
    ) -> Schedule:
        """Add a user-defined scheduled job."""
        async with await get_session() as session:
            async with session.begin():
                schedule = Schedule(
                    user_id=user_id,
                    name=name,
                    description=description,
                    cron_expression=cron_expression,
                    action_type=action_type,
                    action_payload=action_payload,
                    is_system=False,
                )
                session.add(schedule)
                await session.flush()

                # Add to APScheduler
                self._scheduler.add_job(
                    self._execute_user_schedule,
                    CronTrigger.from_crontab(cron_expression),
                    id=f"user_{schedule.id}",
                    args=[str(schedule.id)],
                    replace_existing=True,
                )

                logger.info("schedule.added", name=name, cron=cron_expression)
                return schedule

    async def remove_user_schedule(self, schedule_id: str) -> bool:
        """Remove a user-defined schedule."""
        try:
            self._scheduler.remove_job(f"user_{schedule_id}")
        except Exception:
            pass

        async with await get_session() as session:
            async with session.begin():
                stmt = select(Schedule).where(Schedule.id == schedule_id)
                result = await session.execute(stmt)
                schedule = result.scalar_one_or_none()
                if schedule:
                    schedule.is_active = False
                    return True
                return False

    async def _load_user_schedules(self) -> None:
        """Load all active user schedules from DB into APScheduler."""
        async with await get_session() as session:
            stmt = select(Schedule).where(
                and_(Schedule.is_active == True, Schedule.is_system == False)
            )
            result = await session.execute(stmt)
            schedules = result.scalars().all()

            for sched in schedules:
                try:
                    self._scheduler.add_job(
                        self._execute_user_schedule,
                        CronTrigger.from_crontab(sched.cron_expression),
                        id=f"user_{sched.id}",
                        args=[str(sched.id)],
                        replace_existing=True,
                    )
                except Exception as e:
                    logger.error("schedule.load_failed", id=str(sched.id), error=str(e))

            logger.info("schedules.loaded", count=len(schedules))

    async def _execute_user_schedule(self, schedule_id: str) -> None:
        """Execute a user-defined scheduled action."""
        async with await get_session() as session:
            stmt = select(Schedule).where(Schedule.id == schedule_id)
            result = await session.execute(stmt)
            schedule = result.scalar_one_or_none()

            if not schedule or not schedule.is_active:
                return

            # Get user's telegram_id for notification
            user_stmt = select(User).where(User.id == schedule.user_id)
            user_result = await session.execute(user_stmt)
            user = user_result.scalar_one_or_none()

            if not user:
                return

            # Update last_run
            schedule.last_run_at = datetime.now(timezone.utc)

        # Send notification via Telegram
        if self._telegram_bot and self._telegram_bot._app:
            try:
                message = f"⏰ *Lembrete agendado: {schedule.name}*\n"
                if schedule.description:
                    message += f"{schedule.description}\n"
                message += f"\n_{schedule.action_payload.get('message', '')}_"

                await self._telegram_bot._app.bot.send_message(
                    chat_id=user.telegram_id,
                    text=message,
                    parse_mode="Markdown",
                )
                logger.info("schedule.executed", name=schedule.name, user=user.telegram_id)
            except Exception as e:
                logger.error("schedule.send_failed", error=str(e))

    # ─── SYSTEM JOBS ─────────────────────────────────────────

    async def _job_daily_backup(self) -> None:
        """Create daily backup of context and database."""
        import subprocess
        from pathlib import Path

        backup_dir = Path(self._settings.backup_base_path) / "daily"
        backup_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M")
        backup_file = backup_dir / f"simpleclaw_backup_{timestamp}.tar.gz"

        try:
            subprocess.run(
                ["tar", "-czf", str(backup_file), self._settings.context_base_path],
                check=True,
                timeout=300,
            )

            # Cleanup old backups
            retention = timedelta(days=self._settings.backup_retention_days)
            cutoff = datetime.now() - retention
            for old_file in backup_dir.glob("simpleclaw_backup_*.tar.gz"):
                if datetime.fromtimestamp(old_file.stat().st_mtime) < cutoff:
                    old_file.unlink()

            logger.info("backup.completed", file=str(backup_file))
        except Exception as e:
            logger.error("backup.failed", error=str(e))

    async def _job_history_digest(self) -> None:
        """
        Weekly job: compress old conversations into digests.
        Asks user approval via Telegram before deleting raw messages.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=self._settings.history_compress_after_days)

        async with await get_session() as session:
            # Find users with old conversations
            stmt = (
                select(User.id, User.telegram_id, func.count(Conversation.id))
                .join(Conversation, Conversation.user_id == User.id)
                .where(
                    and_(
                        Conversation.created_at < cutoff,
                        Conversation.is_compressed == False,
                    )
                )
                .group_by(User.id, User.telegram_id)
            )
            result = await session.execute(stmt)
            users_with_old = result.all()

        for user_id, telegram_id, msg_count in users_with_old:
            try:
                await self._create_digest_and_notify(user_id, telegram_id, msg_count, cutoff)
            except Exception as e:
                logger.error("digest.failed", user_id=str(user_id), error=str(e))

    async def _create_digest_and_notify(
        self,
        user_id,
        telegram_id: int,
        msg_count: int,
        cutoff: datetime,
    ) -> None:
        """Create a digest summary and ask user for approval."""
        async with await get_session() as session:
            # Fetch old messages
            stmt = (
                select(Conversation)
                .where(
                    and_(
                        Conversation.user_id == user_id,
                        Conversation.created_at < cutoff,
                        Conversation.is_compressed == False,
                    )
                )
                .order_by(Conversation.created_at)
                .limit(500)
            )
            result = await session.execute(stmt)
            messages = result.scalars().all()

            if not messages:
                return

            # Build summary (extractive for now, model-based in future)
            topics = set()
            for msg in messages:
                content = msg.content.lower()
                for keyword in ["projeto", "tarefa", "banco", "api", "erro", "sucesso", "relatório"]:
                    if keyword in content:
                        topics.add(keyword)

            period_start = messages[0].created_at
            period_end = messages[-1].created_at
            summary = (
                f"Período: {period_start.strftime('%d/%m/%Y')} a {period_end.strftime('%d/%m/%Y')}\n"
                f"Mensagens: {len(messages)}\n"
                f"Tópicos principais: {', '.join(topics) if topics else 'conversa geral'}"
            )

            # Save digest
            digest = HistoryDigest(
                user_id=user_id,
                period_start=period_start,
                period_end=period_end,
                summary=summary,
                key_topics=list(topics),
                message_count=len(messages),
            )
            session.add(digest)
            await session.commit()

        # Notify user
        if self._telegram_bot and self._telegram_bot._app:
            message = (
                f"📚 *Resumo de conversas antigas:*\n\n"
                f"{summary}\n\n"
                f"Deseja manter essas {len(messages)} mensagens na base?\n"
                f"Responda 'manter' ou 'apagar'."
            )
            try:
                await self._telegram_bot._app.bot.send_message(
                    chat_id=telegram_id,
                    text=message,
                    parse_mode="Markdown",
                )
            except Exception as e:
                logger.error("digest.notify_failed", error=str(e))

    async def _job_check_vault_rotation(self) -> None:
        """Check for credentials needing rotation."""
        from src.tools.vault import Vault
        try:
            vault = Vault()
            needs_rotation = await vault.check_rotation_needed()
            if needs_rotation:
                logger.warning("vault.rotation_needed", count=len(needs_rotation))
                # TODO: Notify admins via Telegram
        except Exception as e:
            logger.error("vault.check_failed", error=str(e))
