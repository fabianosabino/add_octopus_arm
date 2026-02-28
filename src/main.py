"""
SimpleClaw v3.0 - Main Entry Point
====================================
Orchestrates initialization and lifecycle of all components.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

import structlog

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.dev.set_exc_info,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(
        structlog.get_config().get("min_level", 20)
    ),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger()


async def main() -> None:
    """Initialize and run SimpleClaw."""
    from src.config.settings import get_settings
    from src.interfaces.telegram_bot import TelegramBot

    settings = get_settings()
    logger.info(
        "simpleclaw.starting",
        version=settings.app_version,
        engine=settings.engine,
        debug=settings.debug,
        model=f"{settings.router_provider.value}/{settings.router_model_id}",
    )

    # Ensure directories exist
    for dir_path in [
        settings.context_base_path,
        f"{settings.context_base_path}/pending",
        f"{settings.context_base_path}/processing",
        f"{settings.context_base_path}/completed",
        settings.backup_base_path,
        settings.log_path,
        settings.sessions_dir,
    ]:
        Path(dir_path).mkdir(parents=True, exist_ok=True)

    # Initialize database (if available)
    try:
        from src.storage.database import init_database, close_database
        await init_database()
        logger.info("simpleclaw.database_ready")
        db_initialized = True
    except Exception as e:
        logger.warning("simpleclaw.database_unavailable", error=str(e)[:200])
        db_initialized = False

    # Initialize scheduler (if available)
    scheduler = None
    try:
        from src.scheduler.cron_jobs import SchedulerService
        scheduler = SchedulerService()
    except Exception:
        logger.warning("simpleclaw.scheduler_unavailable")

    # Initialize Telegram bot
    telegram_bot = TelegramBot()

    # Initialize watchdog (if available)
    watchdog = None
    try:
        from src.tools.watchdog import Watchdog
        watchdog = Watchdog()
        telegram_bot.set_watchdog(watchdog)
        await watchdog.start()
        logger.info("simpleclaw.watchdog_ready")
    except Exception:
        logger.warning("simpleclaw.watchdog_unavailable")

    # Start scheduler
    if scheduler:
        scheduler.set_telegram_bot(telegram_bot)
        await scheduler.start()
        logger.info("simpleclaw.scheduler_ready")

    # Start Telegram bot
    try:
        await telegram_bot.start()
        logger.info("simpleclaw.ready", message="All systems operational 🟢")

        stop_event = asyncio.Event()

        def signal_handler():
            stop_event.set()

        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, signal_handler)

        await stop_event.wait()

    except Exception as e:
        logger.error("simpleclaw.fatal", error=str(e))
    finally:
        logger.info("simpleclaw.shutting_down")
        await telegram_bot.stop()
        if watchdog:
            await watchdog.stop()
        if scheduler:
            await scheduler.stop()
        if db_initialized:
            from src.storage.database import close_database
            await close_database()
        logger.info("simpleclaw.stopped")


def run() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nSimpleClaw stopped.")
        sys.exit(0)


if __name__ == "__main__":
    run()
