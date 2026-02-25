"""
SimpleClaw v2.0 - Context Compressor
======================================
Manages context window size using tiktoken for counting
and model-based summarization for compression.
Replaces the binary RalphCompressor with a reliable approach.
"""

from __future__ import annotations

from typing import Optional

import structlog
import tiktoken

from src.config.settings import get_settings

logger = structlog.get_logger()

# Use cl100k_base as default encoder (works for most models)
_encoder: Optional[tiktoken.Encoding] = None


def get_encoder() -> tiktoken.Encoding:
    global _encoder
    if _encoder is None:
        _encoder = tiktoken.get_encoding("cl100k_base")
    return _encoder


def count_tokens(text: str) -> int:
    """Count tokens in a text string."""
    return len(get_encoder().encode(text))


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Truncate text to a maximum number of tokens."""
    enc = get_encoder()
    tokens = enc.encode(text)
    if len(tokens) <= max_tokens:
        return text
    return enc.decode(tokens[:max_tokens])


class ContextCompressor:
    """
    Manages context window by:
    1. Counting tokens with tiktoken
    2. When threshold is exceeded, uses the router model to summarize
    3. Replaces old context with summary + recent messages
    """

    def __init__(self):
        settings = get_settings()
        self.max_tokens = settings.max_context_tokens
        self.compress_threshold = settings.compress_threshold_tokens

    def needs_compression(self, context: list[dict]) -> bool:
        """Check if context needs compression."""
        total = sum(count_tokens(msg.get("content", "")) for msg in context)
        return total > self.compress_threshold

    def get_context_stats(self, context: list[dict]) -> dict:
        """Get token stats for current context."""
        tokens_per_msg = [
            {"role": msg.get("role"), "tokens": count_tokens(msg.get("content", ""))}
            for msg in context
        ]
        total = sum(m["tokens"] for m in tokens_per_msg)
        return {
            "total_tokens": total,
            "max_tokens": self.max_tokens,
            "threshold": self.compress_threshold,
            "needs_compression": total > self.compress_threshold,
            "usage_pct": round(total / self.max_tokens * 100, 1),
            "message_count": len(context),
        }

    async def compress(
        self,
        context: list[dict],
        model=None,
        keep_recent: int = 10,
    ) -> list[dict]:
        """
        Compress context by summarizing older messages.
        
        Strategy:
        1. Keep the system message (index 0)
        2. Summarize messages from index 1 to -(keep_recent)
        3. Keep the last `keep_recent` messages as-is
        
        Args:
            context: Full message list
            model: Agno model instance for summarization
            keep_recent: Number of recent messages to preserve verbatim
        """
        if not self.needs_compression(context):
            return context

        if len(context) <= keep_recent + 1:
            return context

        system_msg = context[0] if context[0].get("role") == "system" else None
        recent = context[-keep_recent:]
        to_compress = context[1:-keep_recent] if system_msg else context[:-keep_recent]

        if not to_compress:
            return context

        # Build summary text from old messages
        old_text = "\n".join(
            f"[{msg.get('role', 'unknown')}]: {msg.get('content', '')}"
            for msg in to_compress
        )
        old_tokens = count_tokens(old_text)

        if model:
            summary = await self._summarize_with_model(old_text, model)
        else:
            summary = self._extractive_summary(to_compress)

        summary_tokens = count_tokens(summary)
        logger.info(
            "context.compressed",
            original_tokens=old_tokens,
            summary_tokens=summary_tokens,
            reduction_pct=round((1 - summary_tokens / max(old_tokens, 1)) * 100, 1),
            messages_compressed=len(to_compress),
        )

        # Rebuild context
        compressed_msg = {
            "role": "system",
            "content": f"[Resumo do contexto anterior ({len(to_compress)} mensagens)]:\n{summary}",
        }
        result = []
        if system_msg:
            result.append(system_msg)
        result.append(compressed_msg)
        result.extend(recent)
        return result

    async def _summarize_with_model(self, text: str, model) -> str:
        """Use the AI model to create an intelligent summary."""
        from agno.agent import Agent

        summarizer = Agent(
            name="ContextSummarizer",
            model=model,
            instructions=[
                "Você é um compressor de contexto. Resuma a conversa abaixo mantendo:",
                "- Decisões tomadas e conclusões",
                "- Fatos importantes mencionados (nomes, datas, números)",
                "- Tarefas pendentes ou em andamento",
                "- Preferências do usuário identificadas",
                "Descarte: saudações, repetições, tentativas falhas, e conversas casuais.",
                "Responda APENAS com o resumo, sem preâmbulos.",
            ],
            markdown=False,
        )

        # Truncate input to avoid context overflow in summarizer
        truncated = truncate_to_tokens(text, 60_000)
        response = summarizer.run(f"Resuma esta conversa:\n\n{truncated}")
        return response.content if hasattr(response, "content") else str(response)

    def _extractive_summary(self, messages: list[dict], max_chars: int = 5000) -> str:
        """
        Fallback: extractive summary without AI model.
        Keeps first and last messages, plus any with key indicators.
        """
        if not messages:
            return ""

        key_indicators = [
            "decidido", "conclusão", "tarefa", "importante", "lembrar",
            "prazo", "deadline", "resultado", "aprovado", "pendente",
            "erro", "corrigido", "criado", "atualizado", "deletado",
        ]

        important = []
        for msg in messages:
            content = msg.get("content", "").lower()
            if any(kw in content for kw in key_indicators):
                important.append(msg)

        # Always include first and last
        selected = [messages[0]] + important + [messages[-1]]
        # Deduplicate preserving order
        seen = set()
        unique = []
        for msg in selected:
            key = msg.get("content", "")[:100]
            if key not in seen:
                seen.add(key)
                unique.append(msg)

        summary_parts = []
        for msg in unique:
            content = msg.get("content", "")
            if len(content) > 200:
                content = content[:200] + "..."
            summary_parts.append(f"[{msg.get('role', '?')}]: {content}")

        result = "\n".join(summary_parts)
        return result[:max_chars]
