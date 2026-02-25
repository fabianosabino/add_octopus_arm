"""
SimpleClaw v2.0 - Specialist Team
====================================
On-demand specialist agents using Agno Team.
Loads the heavier model only when tasks require it.
Manages RAM by loading/unloading specialist model via Ollama API.
"""

from __future__ import annotations

import asyncio
from typing import Optional

import httpx
import structlog
from agno.agent import Agent
from agno.db.postgres import PostgresDb
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
    """

    def __init__(self):
        self._settings = get_settings()
        self._team: Optional[Team] = None
        self._agents: dict[str, Agent] = {}
        self._model_loaded = False
        self._db: Optional[PostgresDb] = None

    async def preload_model(self) -> bool:
        """
        Preload the specialist model into Ollama memory.
        Called when user interaction starts to reduce latency.
        For API providers (OpenAI, Anthropic), this is a no-op.
        """
        if self._settings.specialist_provider != ModelProvider.OLLAMA:
            self._model_loaded = True
            return True

        try:
            base_url = self._settings.specialist_api_base or "http://localhost:11434"
            async with httpx.AsyncClient(timeout=self._settings.specialist_preload_timeout_seconds) as client:
                # Ollama: sending a minimal request loads the model into memory
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
                    logger.info(
                        "specialist.preloaded",
                        model=self._settings.specialist_model_id,
                    )
                    return True
                else:
                    logger.warning("specialist.preload_failed", status=response.status_code)
                    return False
        except Exception as e:
            logger.error("specialist.preload_error", error=str(e))
            return False

    async def unload_model(self) -> None:
        """
        Unload the specialist model from Ollama to free RAM.
        For API providers, this is a no-op.
        """
        if self._settings.specialist_provider != ModelProvider.OLLAMA:
            return

        try:
            base_url = self._settings.specialist_api_base or "http://localhost:11434"
            async with httpx.AsyncClient(timeout=10) as client:
                # Ollama: keep_alive=0 unloads the model
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

        if not self._db:
            self._db = PostgresDb(
                db_url=self._settings.database_url,
                schema=self._settings.agent_schema,
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
                    db=self._db,
                    add_history_to_context=True,
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

        # Ensure model is loaded
        if not self._model_loaded:
            await self.preload_model()

        self._agents = self._build_agents()

        if not self._agents:
            raise RuntimeError("No specialist agents could be created")

        self._team = Team(
            name="SimpleClaw Specialists",
            mode="coordinate",
            model=self._settings.get_specialist_model_config().get_agno_model(),
            members=list(self._agents.values()),
            db=self._db,
            instructions=[
                "Você coordena uma equipe de especialistas.",
                "Delegue cada parte da tarefa ao especialista mais adequado.",
                "Alexandre cuida de banco de dados e SQL.",
                "Marina cuida de código Python, dashboards e geração de arquivos.",
                "Carlos cuida de infraestrutura, testes e versionamento.",
                "Sophia cuida de pesquisas e validação de informações.",
                "Combine os resultados em uma entrega coesa.",
            ],
            enable_team_history=True,
            show_tool_calls=False,
            markdown=True,
        )

        logger.info("specialist.team_ready", members=len(self._agents))
        return self._team

    async def execute_task(
        self,
        specification: dict,
        user_id: str,
        session_id: str,
    ) -> str:
        """
        Execute a task using the specialist team.
        
        Args:
            specification: Task spec generated by the router
            user_id: Telegram user identifier
            session_id: Conversation session ID
        
        Returns:
            Task result as formatted string
        """
        team = await self.get_team()
        task_description = specification.get("raw_spec", specification.get("original_request", ""))

        try:
            response = team.run(
                task_description,
                user_id=user_id,
                session_id=session_id,
            )
            result = response.content if hasattr(response, "content") else str(response)
            logger.info(
                "specialist.task_completed",
                user_id=user_id,
                result_length=len(result),
            )
            return result
        except Exception as e:
            logger.error("specialist.task_failed", error=str(e), user_id=user_id)
            raise

    async def shutdown(self) -> None:
        """Cleanup: unload model and clear agents."""
        self._team = None
        self._agents = {}
        await self.unload_model()
        logger.info("specialist.shutdown")
