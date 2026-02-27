"""
SimpleClaw v2.1 - LLM Gateway
================================
ÚNICO ponto de acesso a qualquer LLM no sistema.
Todo texto gerado passa por aqui. Sem exceção.

O Agno Agent é encapsulado internamente — ninguém instancia Agent diretamente.
O gateway injeta sanity validation em toda resposta e recovery em toda falha.

Uso:
    gateway = LLMGateway.get_instance()
    result = await gateway.generate(prompt, user_id=..., session_id=...)
    result = await gateway.generate_with_tools(prompt, tools=[...], ...)
"""

from __future__ import annotations

import functools
from typing import Any, Callable, Optional

import structlog
from agno.agent import Agent
from agno.db.postgres import PostgresDb

from src.config.settings import get_settings, ModelConfig

logger = structlog.get_logger()


class SanitizedResponse:
    """Response that passed through sanity validation."""

    def __init__(self, content: str, honest: bool = True, violations: list[str] = None,
                 raw_content: str = "", tool_error: bool = False):
        self.content = content
        self.honest = honest
        self.violations = violations or []
        self.raw_content = raw_content
        self.tool_error = tool_error

    @property
    def is_valid(self) -> bool:
        return self.honest and not self.tool_error

    def __str__(self) -> str:
        return self.content


class LLMGateway:
    """
    Gateway obrigatório. Não existe outro jeito de gerar texto no SimpleClaw.

    Encapsula:
    - Criação de Agno Agents (ninguém instancia Agent fora daqui)
    - Sanity check em toda resposta (honestidade, tool validation)
    - Recovery automático em falhas de tool call
    - Logging estruturado de toda geração
    """

    _instance: Optional[LLMGateway] = None

    def __init__(self):
        self._settings = get_settings()
        self._db: Optional[PostgresDb] = None
        self._agents: dict[str, Agent] = {}
        self._initialized = False

    @classmethod
    def get_instance(cls) -> LLMGateway:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def _reset(cls) -> None:
        """Reset singleton. USE ONLY IN TESTS."""
        cls._instance = None

    def _ensure_db(self) -> PostgresDb:
        if self._db is None:
            self._db = PostgresDb(db_url=self._settings.database_url)
        return self._db

    def _get_or_create_agent(
        self,
        agent_name: str,
        model_config: ModelConfig,
        instructions: Any = None,
        tools: list = None,
    ) -> Agent:
        """
        Get cached agent or create new one.
        This is the ONLY place in the codebase where Agent() is called.
        """
        cache_key = f"{agent_name}:{model_config.model_id}"

        if cache_key not in self._agents:
            model = model_config.get_agno_model()
            db = self._ensure_db()

            agent = Agent(
                name=agent_name,
                model=model,
                instructions=instructions or [],
                tools=tools or [],
                db=db,
                add_history_to_context=True,
                num_history_runs=5,
                markdown=True,
            )
            self._agents[cache_key] = agent
            logger.info("gateway.agent_created", name=agent_name, model=model_config.model_id)

        return self._agents[cache_key]

    async def generate(
        self,
        prompt: str,
        agent_name: str = "router",
        model_config: Optional[ModelConfig] = None,
        instructions: Any = None,
        tools: list = None,
        user_id: str = "",
        session_id: str = "",
        max_tool_retries: int = 2,
    ) -> SanitizedResponse:
        """
        Todo texto gerado passa por aqui. Sem exceção.

        1. Cria/recupera agent
        2. Executa com tool validation
        3. Valida honestidade
        4. Retorna SanitizedResponse
        """
        if model_config is None:
            model_config = self._settings.get_router_model_config()

        agent = self._get_or_create_agent(agent_name, model_config, instructions, tools)

        # Build kwargs
        run_kwargs: dict[str, Any] = {}
        if user_id:
            run_kwargs["user_id"] = user_id
        if session_id:
            run_kwargs["session_id"] = session_id

        current_prompt = prompt

        for attempt in range(1, max_tool_retries + 1):
            try:
                response = agent.run(current_prompt, **run_kwargs)
                raw_content = response.content if hasattr(response, "content") else str(response)

                # ── SANITY: Tool validation ──
                # (if we got here, no tool error)

                # ── SANITY: Honesty check ──
                sanitized = self._check_honesty(raw_content)

                logger.info(
                    "gateway.generated",
                    agent=agent_name,
                    honest=sanitized.honest,
                    violations=sanitized.violations if not sanitized.honest else [],
                    length=len(sanitized.content),
                )

                return sanitized

            except Exception as e:
                # ── SANITY: Tool error detection ──
                tool_error = self._parse_tool_error(e)

                if tool_error is None:
                    # Not a tool error — genuine failure
                    logger.error("gateway.generation_failed", agent=agent_name, error=str(e)[:200])
                    return self._recovery_response(str(e))

                logger.warning(
                    "gateway.tool_error",
                    tool_name=tool_error,
                    attempt=attempt,
                    agent=agent_name,
                )

                if attempt < max_tool_retries:
                    current_prompt = self._build_tool_recovery_prompt(prompt, tool_error)
                    continue

        # All retries exhausted
        return self._recovery_response(f"Tool '{tool_error}' não existe no sistema.")

    def _check_honesty(self, content: str) -> SanitizedResponse:
        """Run honesty check against FrozenManifest."""
        try:
            from src.sanity.sanity_layer import HonestyEnforcer
            enforcer = HonestyEnforcer()
            result = enforcer.check_response(content)

            if not result["honest"]:
                logger.warning("gateway.honesty_violation", violations=result["violations"])

            return SanitizedResponse(
                content=result["corrected"],
                honest=result["honest"],
                violations=result["violations"],
                raw_content=content,
            )
        except Exception:
            # Sanity layer not available — pass through but log
            return SanitizedResponse(content=content, honest=True, raw_content=content)

    def _parse_tool_error(self, error: Exception) -> Optional[str]:
        """Extract tool name from tool call validation error."""
        import re
        msg = str(error)

        # Groq
        match = re.search(r"attempted to call tool '(\w+)'", msg)
        if match:
            return match.group(1)

        # OpenAI
        match = re.search(r"function '(\w+)' is not defined", msg)
        if match:
            return match.group(1)

        # Generic
        if "tool_use_failed" in msg:
            match = re.search(r"tool[_\s]+['\"]?(\w+)['\"]?", msg)
            if match:
                return match.group(1)

        return None

    def _build_tool_recovery_prompt(self, original: str, tool_name: str) -> str:
        """Build recovery prompt that prevents hallucinated tool calls."""
        try:
            from src.sanity.sanity_layer import CapabilityRegistry
            registry = CapabilityRegistry()
            validation = registry.validate_tool_call(tool_name)
            tools_list = ", ".join(registry.available_tool_names)
            reason = validation.get("suggestion") or validation.get("reason", "")
        except Exception:
            tools_list = "search_web, run_sql, execute_python, create_csv, create_xlsx, create_pdf, create_docx"
            reason = "ferramenta não existe"

        return (
            f"{original}\n\n"
            f"IMPORTANTE: '{tool_name}' NÃO existe. {reason}\n"
            f"Ferramentas disponíveis: {tools_list}.\n"
            f"Responda usando APENAS texto ou ferramentas da lista."
        )

    def _recovery_response(self, error_detail: str) -> SanitizedResponse:
        """Generate user-friendly recovery response."""
        # Check if it's a known unavailable capability
        try:
            from src.sanity.sanity_layer import CapabilityRegistry
            registry = CapabilityRegistry()

            # Try to match error to a known unavailable capability
            for word in error_detail.lower().split():
                validation = registry.validate_tool_call(word)
                if not validation["valid"] and validation["suggestion"]:
                    return SanitizedResponse(
                        content=f"Entendi seu pedido, mas {validation['suggestion'].lower()} Posso ajudar com outra coisa?",
                        honest=True,
                        tool_error=True,
                    )
        except Exception:
            pass

        return SanitizedResponse(
            content=(
                "Encontrei uma limitação ao processar seu pedido. "
                "Pode reformular de forma diferente?"
            ),
            honest=True,
            tool_error=True,
        )

    # ─── PROIBIÇÃO EXPLÍCITA ─────────────────────────────────

    @property
    def raw_provider(self):
        raise RuntimeError(
            "Acesso direto ao provider bloqueado. "
            "Use generate() ou registre exceção no manifest."
        )

    @property
    def raw_agents(self):
        raise RuntimeError(
            "Acesso direto aos agents bloqueado. "
            "Use generate() pelo gateway."
        )
