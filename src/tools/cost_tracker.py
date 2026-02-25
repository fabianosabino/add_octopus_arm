"""
SimpleClaw v2.0 - Cost Tracker
================================
Tracks token usage and estimates costs per provider/model.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

import structlog
from sqlalchemy import select, func, and_

from src.config.settings import get_settings
from src.storage.database import get_session
from src.storage.models import CostLog

logger = structlog.get_logger()

# ─── PRICING TABLE (USD per 1M tokens) ─────────────────────
# Updated as needed. For local models (Ollama), cost is 0.
PRICING = {
    # OpenAI
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4.1": {"input": 2.00, "output": 8.00},
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
    "gpt-4.1-nano": {"input": 0.10, "output": 0.40},
    # Anthropic
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-sonnet-4-5-20250929": {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    "claude-opus-4-6": {"input": 15.00, "output": 75.00},
    # Groq (free tier / cheap)
    "llama-3.3-70b-versatile": {"input": 0.59, "output": 0.79},
    "llama-3.1-8b-instant": {"input": 0.05, "output": 0.08},
    # Local (Ollama) - free
    "_default_local": {"input": 0.0, "output": 0.0},
}


def estimate_cost(model_id: str, input_tokens: int, output_tokens: int, provider: str = "") -> float:
    """Estimate cost in USD for a given model and token usage."""
    if provider == "ollama":
        return 0.0

    pricing = PRICING.get(model_id, PRICING.get("_default_local"))
    cost = (input_tokens * pricing["input"] / 1_000_000) + (output_tokens * pricing["output"] / 1_000_000)
    return round(cost, 6)


async def log_usage(
    user_id: uuid.UUID,
    provider: str,
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    request_type: str = "chat",
    task_id: Optional[uuid.UUID] = None,
) -> CostLog:
    """Log token usage and estimated cost."""
    total = input_tokens + output_tokens
    cost = estimate_cost(model_id, input_tokens, output_tokens, provider)

    async with await get_session() as session:
        async with session.begin():
            entry = CostLog(
                user_id=user_id,
                task_id=task_id,
                provider=provider,
                model_id=model_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total,
                estimated_cost_usd=cost,
                request_type=request_type,
            )
            session.add(entry)

    if cost > 0:
        logger.info(
            "cost.logged",
            user_id=str(user_id),
            model=model_id,
            tokens=total,
            cost_usd=cost,
        )

    return entry


async def get_user_cost_summary(
    user_id: uuid.UUID,
    days: int = 30,
) -> dict:
    """Get cost summary for a user over the last N days."""
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    async with await get_session() as session:
        stmt = select(
            func.sum(CostLog.total_tokens).label("total_tokens"),
            func.sum(CostLog.estimated_cost_usd).label("total_cost"),
            func.count(CostLog.id).label("request_count"),
            CostLog.provider,
            CostLog.model_id,
        ).where(
            and_(
                CostLog.user_id == user_id,
                CostLog.created_at >= cutoff,
            )
        ).group_by(CostLog.provider, CostLog.model_id)

        result = await session.execute(stmt)
        rows = result.all()

    total_cost = sum(r.total_cost or 0 for r in rows)
    total_tokens = sum(r.total_tokens or 0 for r in rows)

    return {
        "period_days": days,
        "total_cost_usd": round(total_cost, 4),
        "total_tokens": total_tokens,
        "total_requests": sum(r.request_count for r in rows),
        "by_model": [
            {
                "provider": r.provider,
                "model": r.model_id,
                "tokens": r.total_tokens or 0,
                "cost_usd": round(r.total_cost or 0, 4),
                "requests": r.request_count,
            }
            for r in rows
        ],
    }
