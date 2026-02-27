"""
SimpleClaw v3.0 - LLM Client
===============================
Cliente direto para qualquer API compatível com OpenAI chat completions.
Sem framework intermediário. Sem Agno. Sem URLs hardcoded.

Provider, modelo, API key e URL base vêm 100% do .env / settings.
Faz uma coisa: manda mensagens + tools pro LLM e retorna a resposta.
O agent loop decide o que fazer com a resposta.

Configuração .env:
    SIMPLECLAW_ROUTER_PROVIDER=groq
    SIMPLECLAW_ROUTER_API_BASE=https://api.groq.com/openai/v1
    SIMPLECLAW_ROUTER_API_KEY=gsk_...
    SIMPLECLAW_ROUTER_MODEL_ID=llama-3.1-8b-instant
"""

from __future__ import annotations

import json
from typing import Any, Optional

import httpx
import structlog

from src.config.settings import get_settings

logger = structlog.get_logger()


class LLMResponse:
    """Parsed response from LLM API."""

    def __init__(self, content: str = "", tool_calls: list[dict] = None,
                 finish_reason: str = "", usage: dict = None, raw: dict = None):
        self.content = content
        self.tool_calls = tool_calls or []
        self.finish_reason = finish_reason  # "stop", "tool_calls", "length"
        self.usage = usage or {}
        self.raw = raw or {}

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0

    @property
    def is_final(self) -> bool:
        return not self.has_tool_calls


class LLMClient:
    """
    Cliente direto para APIs LLM.
    Stateless — não guarda histórico, não gerencia sessão.
    O agent loop cuida disso.
    """

    def __init__(self, provider: str = None, model_id: str = None,
                 api_key: str = None, api_base: str = None,
                 temperature: float = None, max_tokens: int = None):
        settings = get_settings()

        self.provider = provider or settings.router_provider.value
        self.model_id = model_id or settings.router_model_id
        self.api_key = api_key or settings.router_api_key or ""
        self.temperature = temperature if temperature is not None else settings.router_temperature
        self.max_tokens = max_tokens or settings.router_max_tokens

        # URL base vem EXCLUSIVAMENTE do settings/env
        # Deve apontar pra raiz da API (ex: https://api.groq.com/openai/v1)
        # O client adiciona /chat/completions automaticamente
        raw_base = api_base or settings.router_api_base or ""

        if not raw_base:
            raise LLMConfigError(
                f"SIMPLECLAW_ROUTER_API_BASE não configurado. "
                f"Defina a URL base da API no .env. "
                f"Exemplos:\n"
                f"  Groq:   https://api.groq.com/openai/v1\n"
                f"  OpenAI: https://api.openai.com/v1\n"
                f"  Ollama: http://localhost:11434/v1\n"
                f"  Custom: https://sua-api.com/v1"
            )

        # Normaliza: garante que termina sem / e adiciona /chat/completions
        raw_base = raw_base.rstrip("/")
        if raw_base.endswith("/chat/completions"):
            self.base_url = raw_base
        else:
            self.base_url = f"{raw_base}/chat/completions"

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] = None,
        temperature: float = None,
        max_tokens: int = None,
    ) -> LLMResponse:
        """
        Send messages to LLM and return response.

        Args:
            messages: OpenAI-format messages [{"role": "user", "content": "..."}]
            tools: OpenAI-format tool definitions (optional)
            temperature: Override default temperature
            max_tokens: Override default max_tokens

        Returns:
            LLMResponse with content and/or tool_calls
        """
        payload: dict[str, Any] = {
            "model": self.model_id,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.temperature,
            "max_tokens": max_tokens or self.max_tokens,
        }

        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        try:
            async with httpx.AsyncClient(timeout=120) as client:
                response = await client.post(
                    self.base_url,
                    headers=headers,
                    json=payload,
                )

            if response.status_code != 200:
                error_text = response.text[:500]
                logger.error("llm_client.api_error", status=response.status_code, body=error_text)
                raise LLMError(f"API error {response.status_code}: {error_text}")

            data = response.json()
            return self._parse_response(data)

        except httpx.TimeoutException:
            raise LLMError("LLM request timed out (120s)")
        except LLMError:
            raise
        except Exception as e:
            raise LLMError(f"LLM request failed: {str(e)}")

    def _parse_response(self, data: dict) -> LLMResponse:
        """Parse OpenAI-compatible API response."""
        choices = data.get("choices", [])
        if not choices:
            return LLMResponse(content="", raw=data)

        choice = choices[0]
        message = choice.get("message", {})
        finish_reason = choice.get("finish_reason", "stop")

        content = message.get("content", "") or ""
        tool_calls = []

        if message.get("tool_calls"):
            for tc in message["tool_calls"]:
                func = tc.get("function", {})
                try:
                    args = json.loads(func.get("arguments", "{}"))
                except json.JSONDecodeError:
                    args = {}

                tool_calls.append({
                    "id": tc.get("id", ""),
                    "name": func.get("name", ""),
                    "arguments": args,
                })

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=data.get("usage", {}),
            raw=data,
        )


class LLMError(Exception):
    """Error from LLM API call."""
    pass


class LLMConfigError(Exception):
    """Error from missing or invalid LLM configuration."""
    pass
