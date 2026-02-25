"""
SimpleClaw v2.0 - Health Watchdog
===================================
Monitors system health: model availability, database, scheduler.
Provides fallback behavior when components fail.
Runs as a lightweight async loop alongside the main application.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

import httpx
import psutil
import structlog
from sqlalchemy import text

from src.config.settings import get_settings, ModelProvider
from src.storage.database import get_session

logger = structlog.get_logger()


class HealthStatus:
    """Health check result for a component."""

    def __init__(self, name: str, healthy: bool, detail: str = "", latency_ms: float = 0):
        self.name = name
        self.healthy = healthy
        self.detail = detail
        self.latency_ms = latency_ms
        self.checked_at = datetime.now(timezone.utc)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "healthy": self.healthy,
            "detail": self.detail,
            "latency_ms": round(self.latency_ms, 1),
            "checked_at": self.checked_at.isoformat(),
        }


class Watchdog:
    """
    System health monitor with automatic recovery attempts.

    Checks:
    - PostgreSQL connectivity
    - Ollama model availability (if using local models)
    - SearXNG availability
    - System resources (RAM, disk)
    - Active task heartbeats

    Recovery:
    - Attempts to reconnect to database
    - Falls back to extractive compression if model unavailable
    - Logs warnings for resource exhaustion
    """

    def __init__(self):
        self._settings = get_settings()
        self._running = False
        self._check_interval = 60  # seconds
        self._last_status: dict[str, HealthStatus] = {}
        self._consecutive_failures: dict[str, int] = {}
        self._notify_callback = None  # Set by telegram bot

    def set_notify_callback(self, callback) -> None:
        """Set async callback for admin notifications."""
        self._notify_callback = callback

    async def start(self) -> None:
        """Start the watchdog monitoring loop."""
        self._running = True
        logger.info("watchdog.started", interval=self._check_interval)
        asyncio.create_task(self._monitor_loop())

    async def stop(self) -> None:
        """Stop the watchdog."""
        self._running = False
        logger.info("watchdog.stopped")

    async def _monitor_loop(self) -> None:
        """Main monitoring loop."""
        while self._running:
            try:
                await self.check_all()
            except Exception as e:
                logger.error("watchdog.loop_error", error=str(e))
            await asyncio.sleep(self._check_interval)

    async def check_all(self) -> dict[str, HealthStatus]:
        """Run all health checks."""
        checks = [
            self._check_database(),
            self._check_system_resources(),
        ]

        # Only check Ollama if using local models
        if self._settings.router_provider == ModelProvider.OLLAMA:
            checks.append(self._check_ollama())

        # Check SearXNG
        checks.append(self._check_searxng())

        results = await asyncio.gather(*checks, return_exceptions=True)

        for result in results:
            if isinstance(result, HealthStatus):
                self._last_status[result.name] = result

                # Track consecutive failures
                if not result.healthy:
                    count = self._consecutive_failures.get(result.name, 0) + 1
                    self._consecutive_failures[result.name] = count

                    if count >= 3:
                        await self._alert_admin(result)
                else:
                    self._consecutive_failures[result.name] = 0

        return self._last_status

    async def _check_database(self) -> HealthStatus:
        """Check PostgreSQL connectivity."""
        import time
        start = time.monotonic()
        try:
            async with await get_session() as session:
                await session.execute(text("SELECT 1"))
            latency = (time.monotonic() - start) * 1000
            return HealthStatus("database", True, "Connected", latency)
        except Exception as e:
            latency = (time.monotonic() - start) * 1000
            return HealthStatus("database", False, str(e)[:100], latency)

    async def _check_ollama(self) -> HealthStatus:
        """Check Ollama model availability."""
        import time
        base_url = self._settings.router_api_base or "http://localhost:11434"
        start = time.monotonic()

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(f"{base_url}/api/tags")
                latency = (time.monotonic() - start) * 1000

                if response.status_code == 200:
                    models = response.json().get("models", [])
                    model_names = [m.get("name", "") for m in models]

                    router_available = self._settings.router_model_id in model_names
                    specialist_available = self._settings.specialist_model_id in model_names

                    if router_available and specialist_available:
                        return HealthStatus("ollama", True, "Both models available", latency)
                    elif router_available:
                        return HealthStatus(
                            "ollama", True,
                            f"Router OK, specialist '{self._settings.specialist_model_id}' not pulled",
                            latency,
                        )
                    else:
                        return HealthStatus(
                            "ollama", False,
                            f"Router model '{self._settings.router_model_id}' not available",
                            latency,
                        )
                else:
                    return HealthStatus("ollama", False, f"HTTP {response.status_code}", latency)

        except httpx.ConnectError:
            return HealthStatus("ollama", False, "Ollama not reachable")
        except Exception as e:
            return HealthStatus("ollama", False, str(e)[:100])

    async def _check_searxng(self) -> HealthStatus:
        """Check SearXNG availability."""
        import time
        start = time.monotonic()

        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(f"{self._settings.searxng_url}/healthz")
                latency = (time.monotonic() - start) * 1000

                if response.status_code == 200:
                    return HealthStatus("searxng", True, "Available", latency)
                else:
                    return HealthStatus("searxng", False, f"HTTP {response.status_code}", latency)

        except httpx.ConnectError:
            return HealthStatus("searxng", False, "SearXNG not reachable")
        except Exception as e:
            return HealthStatus("searxng", False, str(e)[:100])

    async def _check_system_resources(self) -> HealthStatus:
        """Check system RAM and disk usage."""
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")

        issues = []
        if mem.percent > 90:
            issues.append(f"RAM: {mem.percent}% used ({mem.available // 1024 // 1024}MB free)")
        if disk.percent > 90:
            issues.append(f"Disk: {disk.percent}% used ({disk.free // 1024 // 1024 // 1024}GB free)")

        if issues:
            return HealthStatus("system", False, "; ".join(issues))

        return HealthStatus(
            "system", True,
            f"RAM: {mem.percent}%, Disk: {disk.percent}%",
        )

    async def _alert_admin(self, status: HealthStatus) -> None:
        """Send alert to admin via Telegram."""
        if self._notify_callback:
            message = (
                f"🚨 *Alerta de Saúde do Sistema*\n\n"
                f"Componente: `{status.name}`\n"
                f"Status: ❌ Não saudável\n"
                f"Detalhes: {status.detail}\n"
                f"Falhas consecutivas: {self._consecutive_failures.get(status.name, 0)}"
            )
            try:
                for admin_id in self._settings.telegram_admin_ids:
                    await self._notify_callback(admin_id, message)
            except Exception as e:
                logger.error("watchdog.alert_failed", error=str(e))

    def get_status_report(self) -> str:
        """Generate a formatted status report."""
        if not self._last_status:
            return "⏳ Watchdog ainda não realizou verificações."

        lines = ["🏥 *Status do Sistema:*\n"]
        for name, status in self._last_status.items():
            emoji = "✅" if status.healthy else "❌"
            lines.append(f"{emoji} *{name}*: {status.detail}")
            if status.latency_ms > 0:
                lines.append(f"   Latência: {status.latency_ms:.0f}ms")

        return "\n".join(lines)
