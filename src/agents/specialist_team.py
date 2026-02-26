"""
SimpleClaw v2.0 - Specialist Team
====================================
On-demand specialist agents using Agno Team.
Loads the heavier model only when tasks require it.
Manages RAM by loading/unloading specialist model via Ollama API.
"""

from __future__ import annotations

import asyncio
import traceback
from typing import Optional

import httpx
import structlog
from agno.agent import Agent
from agno.storage.postgres import PostgresStorage
from agno.team.team import Team

from src.config.settings import get_settings, ModelProvider
from src.config.persona_loader import (
    load_persona,
    build_agent_instructions,
    build_agent_description,
)

logger = structlog.get_logger()


class SpecialistManager:
    """
    Manages the specialist model lifecycle and Agno Team.
    
    - Preloads model on interaction start (reduces cold start)
    - Creates specialist agents with distinct personas
    - Coordinates via Agno Team mode "coordinate"
    - Unloads model after task completion to free RAM
    - Retry with fallback on failure
    - Step-by-step progress notification
    """

    def __init__(self):
        self._settings = get_settings()
        self._team: Optional[Team] = None
        self._agents: dict[str, Agent] = {}
        self._model_loaded = False
        self._storage: Optional[PostgresStorage] = None
        self._notify_callback = None  # Set by telegram bot for progress updates

    def set_notify_callback(self, callback) -> None:
        """Set async callback for sending progress updates to user."""
        self._notify_callback = callback

    async def _notify_user(self, chat_id: int, message: str) -> None:
        """Send progress update to user if callback is set."""
        if self._notify_callback:
            try:
                await self._notify_callback(chat_id, message)
            except Exception:
                pass

    async def preload_model(self) -> bool:
        """
        Preload the specialist model into Ollama memory.
        For API providers (OpenAI, Anthropic, Groq), this is a no-op.
        """
        if self._settings.specialist_provider != ModelProvider.OLLAMA:
            self._model_loaded = True
            return True

        try:
            base_url = self._settings.specialist_api_base or "http://localhost:11434"
            async with httpx.AsyncClient(timeout=self._settings.specialist_preload_timeout_seconds) as client:
                response = await client.post(
                    f"{base_url}/api/generate",
                    json={
                        "model": self._settings.specialist_model_id,
                        "prompt": "ok",
                        "options": {"num_predict": 1},
                    },
                )
                if response.status_code == 200:
                    self._model_loaded = True
                    logger.info("specialist.preloaded", model=self._settings.specialist_model_id)
                    return True
                else:
                    logger.warning("specialist.preload_failed", status=response.status_code)
                    return False
        except Exception as e:
            logger.error("specialist.preload_error", error=str(e))
            return False

    async def unload_model(self) -> None:
        """Unload the specialist model from Ollama to free RAM."""
        if self._settings.specialist_provider != ModelProvider.OLLAMA:
            return

        try:
            base_url = self._settings.specialist_api_base or "http://localhost:11434"
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"{base_url}/api/generate",
                    json={
                        "model": self._settings.specialist_model_id,
                        "prompt": "",
                        "keep_alive": 0,
                    },
                )
            self._model_loaded = False
            logger.info("specialist.unloaded", model=self._settings.specialist_model_id)
        except Exception as e:
            logger.error("specialist.unload_error", error=str(e))

    def _build_agents(self) -> dict[str, Agent]:
        """Build all specialist agents with their personas."""
        model_config = self._settings.get_specialist_model_config()
        model = model_config.get_agno_model()

        if not self._storage:
            self._storage = PostgresStorage(
                table_name="specialist_sessions",
                db_url=self._settings.database_url,
            )

        from src.tools.agno_wrappers import (
            get_db_architect_tools,
            get_code_wizard_tools,
            get_devops_tools,
            get_research_tools,
        )

        tool_map = {
            "db_architect": get_db_architect_tools(),
            "code_wizard": get_code_wizard_tools(),
            "devops_engineer": get_devops_tools(),
            "research_analyst": get_research_tools(),
        }

        agents = {}
        for name in tool_map.keys():
            try:
                persona = load_persona(name)
                agent = Agent(
                    name=persona.get("name", name),
                    role=persona.get("role", "Specialist"),
                    model=model,
                    instructions=build_agent_instructions(persona),
                    description=build_agent_description(persona),
                    tools=tool_map[name],
                    storage=self._storage,
                    add_history_to_messages=True,
                    num_history_runs=3,
                    markdown=True,
                )
                agents[name] = agent
                logger.info("specialist.created", name=name, role=persona.get("role"))
            except Exception as e:
                logger.error("specialist.create_failed", name=name, error=str(e))

        return agents

    async def get_team(self) -> Team:
        """Get or create the specialist team."""
        if self._team is not None:
            return self._team

        if not self._model_loaded:
            await self.preload_model()

        self._agents = self._build_agents()

        if not self._agents:
            raise RuntimeError("No specialist agents could be created")

        team_storage = PostgresStorage(
            table_name="team_sessions",
            db_url=self._settings.database_url,
        )

        self._team = Team(
            name="SimpleClaw Specialists",
            mode="coordinate",
            model=self._settings.get_specialist_model_config().get_agno_model(),
            members=list(self._agents.values()),
            storage=team_storage,
            instructions=[
                "Você coordena uma equipe de especialistas.",
                "Delegue cada parte da tarefa ao especialista mais adequado.",
                "Alexandre cuida de banco de dados e SQL.",
                "Marina cuida de código Python, dashboards e geração de arquivos.",
                "Carlos cuida de infraestrutura, testes e versionamento.",
                "Sophia cuida de pesquisas e validação de informações.",
                "Combine os resultados em uma entrega coesa.",
            ],
            markdown=True,
        )

        logger.info("specialist.team_ready", members=len(self._agents))
        return self._team

    async def execute_task(
        self,
        specification: dict,
        user_id: str,
        session_id: str,
        chat_id: int = 0,
        max_retries: int = 2,
    ) -> str:
        """
        Execute a task with retry logic and progress notification.
        
        Args:
            specification: Task spec generated by the router
            user_id: User identifier
            session_id: Conversation session ID
            chat_id: Telegram chat ID for progress notifications
            max_retries: Number of retry attempts on failure
        
        Returns:
            Task result as formatted string
        """
        team = await self.get_team()
        task_description = specification.get("raw_spec", specification.get("original_request", ""))
        last_error = None

        for attempt in range(1, max_retries + 1):
            try:
                if attempt > 1:
                    await self._notify_user(
                        chat_id,
                        f"🔄 Tentativa {attempt}/{max_retries}. Ajustando abordagem..."
                    )
                    # Clear team to force rebuild on retry
                    self._team = None
                    team = await self.get_team()

                response = team.run(
                    task_description,
                    user_id=user_id,
                    session_id=session_id,
                )
                result = response.content if hasattr(response, "content") else str(response)
                logger.info(
                    "specialist.task_completed",
                    user_id=user_id,
                    attempt=attempt,
                    result_length=len(result),
                )
                return result

            except Exception as e:
                last_error = e
                error_detail = str(e)[:200]
                logger.error(
                    "specialist.task_attempt_failed",
                    attempt=attempt,
                    max_retries=max_retries,
                    error=error_detail,
                    traceback=traceback.format_exc()[:500],
                )

                if attempt < max_retries:
                    await asyncio.sleep(2 * attempt)  # Backoff
                    continue

        # All retries exhausted
        logger.error("specialist.task_exhausted", user_id=user_id, error=str(last_error))

        # Return user-friendly message instead of raw error
        return (
            "⚠️ Encontrei dificuldades ao executar esta tarefa após múltiplas tentativas.\n\n"
            f"Problema identificado: {str(last_error)[:150]}\n\n"
            "Sugestões:\n"
            "• Tente reformular o pedido com mais detalhes\n"
            "• Divida a tarefa em partes menores\n"
            "• Use /status para verificar o estado do sistema"
        )

    async def shutdown(self) -> None:
        """Cleanup: unload model and clear agents."""
        self._team = None
        self._agents = {}
        await self.unload_model()
        logger.info("specialist.shutdown")
