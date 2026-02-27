"""
SimpleClaw v3.0 - Engine Adapter
====================================
Ponto de comutação entre Agno (legado) e Agent Loop (novo).

Configuração via .env:
    SIMPLECLAW_ENGINE=loop   # novo agent loop (padrão)
    SIMPLECLAW_ENGINE=agno   # fallback pra Agno

O telegram_bot chama o adapter em vez de falar direto com router/specialist.
Quando o loop estiver estável, remove o Agno e este adapter vira desnecessário.
"""

from __future__ import annotations

import asyncio
from typing import Callable, Optional

import structlog

from src.config.settings import get_settings

logger = structlog.get_logger()


class EngineAdapter:
    """
    Facade que abstrai o engine de execução.
    O telegram_bot fala com isto, não com router/loop diretamente.
    """

    def __init__(self):
        self._settings = get_settings()
        self._engine_type = getattr(self._settings, "engine", "loop")
        self._initialized = False

        # Loop engine
        self._loop = None
        self._tool_registry = None
        self._session_store = None

        # Agno engine (legacy)
        self._router = None
        self._specialist = None
        self._task_executor = None

    async def initialize(self) -> None:
        """Initialize the selected engine."""
        if self._initialized:
            return

        if self._engine_type == "loop":
            await self._init_loop()
        else:
            await self._init_agno()

        self._initialized = True
        logger.info("engine.initialized", type=self._engine_type)

    async def _init_loop(self) -> None:
        """Initialize the agent loop engine."""
        from src.core.llm_client import LLMClient
        from src.core.tool_registry import build_default_registry
        from src.core.session_store import SessionStore
        from src.core.agent_loop import AgentLoop, build_system_prompt

        self._tool_registry = build_default_registry()
        self._session_store = SessionStore()

        system_prompt = build_system_prompt()

        self._loop = AgentLoop(
            system_prompt=system_prompt,
            llm_client=LLMClient(),
            tool_registry=self._tool_registry,
            session_store=self._session_store,
        )

        logger.info(
            "engine.loop_ready",
            tools=self._tool_registry.get_tool_names(),
        )

    async def _init_agno(self) -> None:
        """Initialize the Agno engine (legacy fallback)."""
        from src.agents.router import RouterAgent
        from src.agents.specialist_team import SpecialistManager
        from src.agents.task_executor import TaskExecutor

        self._router = RouterAgent()
        self._specialist = SpecialistManager()
        self._task_executor = TaskExecutor(self._specialist, self._router)

        await self._router.initialize()
        logger.info("engine.agno_ready")

    async def chat(self, message: str, user_id: str, session_id: str,
                   chat_id: int = 0) -> str:
        """
        Process a user message. Routes to the correct engine.
        """
        if not self._initialized:
            await self.initialize()

        if self._engine_type == "loop":
            return await self._chat_loop(message, user_id, session_id)
        else:
            return await self._chat_agno(message, user_id, session_id, chat_id)

    async def _chat_loop(self, message: str, user_id: str, session_id: str) -> str:
        """Process via agent loop."""
        try:
            # Pre-validation: check if intent is possible BEFORE sending to LLM
            blocked = self._pre_validate(message)
            if blocked:
                return blocked

            result = await self._loop.run(
                user_message=message,
                user_id=user_id,
                session_id=session_id,
            )
            return result

        except Exception as e:
            logger.error("engine.loop_error", error=str(e))

            # Fallback to Agno if available
            if self._router:
                logger.info("engine.fallback_to_agno")
                return await self._chat_agno(message, user_id, session_id)

            return (
                "Encontrei um problema ao processar sua mensagem. "
                "Tente novamente em alguns segundos."
            )

    async def _chat_agno(self, message: str, user_id: str, session_id: str,
                         chat_id: int = 0) -> str:
        """Process via Agno (legacy)."""
        try:
            from src.agents.router import Intent

            intent = await self._router.classify_intent(message)

            if intent == Intent.TASK:
                return await self._task_executor.execute(
                    request=message,
                    user_id=user_id,
                    session_id=session_id,
                    chat_id=chat_id,
                )
            else:
                return await self._router.chat(message, user_id, session_id)

        except Exception as e:
            logger.error("engine.agno_error", error=str(e))
            return "Encontrei um problema. Tente novamente."

    def _pre_validate(self, message: str) -> Optional[str]:
        """
        Sanity Layer as GATEKEEPER, not editor.
        Blocks BEFORE sending to LLM if we know it can't be handled.
        """
        try:
            from src.sanity.sanity_layer import CapabilityRegistry

            registry = CapabilityRegistry()
            lower = message.lower()

            # Meta-cognitive requests
            meta_patterns = [
                "teste seus limites", "analise seu código",
                "mostre seu prompt", "qual seu modelo",
                "faça auto-análise", "teste seu potencial",
            ]
            for pattern in meta_patterns:
                if pattern in lower:
                    return (
                        "Não posso realizar auto-análise. "
                        "Posso executar tarefas concretas como criar arquivos, "
                        "bancos de dados ou dashboards. Qual você precisa?"
                    )

            # Schedule requests (known unavailable)
            schedule_patterns = ["agendar", "lembrar às", "alarme", "todo dia às"]
            for pattern in schedule_patterns:
                if pattern in lower:
                    validation = registry.validate_tool_call("schedule_message")
                    if not validation["valid"]:
                        return f"{validation.get('suggestion', 'Agendamento ainda não implementado.')} Posso ajudar com outra coisa?"

        except ImportError:
            pass

        return None  # Passed validation

    @property
    def engine_type(self) -> str:
        return self._engine_type

    @property
    def session_store(self) -> Optional[object]:
        return self._session_store

    async def shutdown(self) -> None:
        """Cleanup."""
        if self._specialist:
            await self._specialist.shutdown()
