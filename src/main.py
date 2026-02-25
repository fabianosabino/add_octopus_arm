"""
SimpleClaw v2.0 - Main Entry Point
====================================
Orchestrates initialization and lifecycle of all components.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

import structlog

# Configure structured logging
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
    from src.storage.database import init_database, close_database
    from src.interfaces.telegram_bot import TelegramBot
    from src.scheduler.cron_jobs import SchedulerService
    from src.tools.watchdog import Watchdog

    settings = get_settings()
    logger.info(
        "simpleclaw.starting",
        version=settings.app_version,
        debug=settings.debug,
        router_model=f"{settings.router_provider.value}/{settings.router_model_id}",
        specialist_model=f"{settings.specialist_provider.value}/{settings.specialist_model_id}",
    )

    # Ensure directories exist
    for dir_path in [
        settings.context_base_path,
        f"{settings.context_base_path}/pending",
        f"{settings.context_base_path}/processing",
        f"{settings.context_base_path}/completed",
        f"{settings.context_base_path}/interrupt",
        settings.backup_base_path,
        f"{settings.backup_base_path}/daily",
        f"{settings.backup_base_path}/pre_task",
        settings.log_path,
    ]:
        Path(dir_path).mkdir(parents=True, exist_ok=True)

    # Initialize database
    await init_database()
    logger.info("simpleclaw.database_ready")

    # Initialize scheduler
    scheduler = SchedulerService()

    # Initialize Telegram bot
    telegram_bot = TelegramBot()

    # Initialize watchdog
    watchdog = Watchdog()
    telegram_bot.set_watchdog(watchdog)

    # Wire scheduler -> telegram for notifications
    scheduler.set_telegram_bot(telegram_bot)

    # Start watchdog
    await watchdog.start()
    logger.info("simpleclaw.watchdog_ready")

    # Start scheduler
    await scheduler.start()
    logger.info("simpleclaw.scheduler_ready")

    # Start Telegram bot (blocking)
    try:
        await telegram_bot.start()
        logger.info("simpleclaw.ready", message="All systems operational 🟢")

        # Keep running until interrupted
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
        await watchdog.stop()
        await scheduler.stop()
        await close_database()
        logger.info("simpleclaw.stopped")


def run() -> None:
    """Entry point for the application."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nSimpleClaw stopped.")
        sys.exit(0)


if __name__ == "__main__":
    run()
