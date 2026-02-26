"""
SimpleClaw v2.0 - Router Agent
================================
Main orchestrator using Agno framework.
Classifies intent, manages handoffs, and handles direct chat.
Always-on with the lightweight model.
"""

from __future__ import annotations

import uuid
from enum import Enum
from typing import Optional

import structlog
from agno.agent import Agent
from agno.storage.postgres import PostgresStorage

from src.config.settings import get_settings
from src.config.persona_loader import load_persona, build_agent_instructions

logger = structlog.get_logger()


class Intent(str, Enum):
    """Classified user intents."""
    CHAT = "chat"
    TASK = "task"
    STATUS = "status"
    SCHEDULE = "schedule"
    COMMAND = "command"
    FILE_REQUEST = "file_request"
    DB_QUERY = "db_query"
    SEARCH = "search"
    UNKNOWN = "unknown"


class RouterAgent:
    """
    Main orchestrator agent. Runs on the lightweight model (always-on).
    
    Responsibilities:
    - Classify user intent
    - Handle direct chat
    - Generate task specifications for specialist delegation
    - Manage conversation history per user
    - Coordinate model preloading for specialist tasks
    """

    def __init__(self):
        self._settings = get_settings()
        self._persona = load_persona("router")
        self._agent: Optional[Agent] = None
        self._storage: Optional[PostgresStorage] = None
        self._initialized = False

    async def initialize(self) -> None:
        """Initialize the router agent with Agno components."""
        if self._initialized:
            return

        self._storage = PostgresStorage(
            table_name="router_sessions",
            db_url=self._settings.database_url,
        )

        model_config = self._settings.get_router_model_config()
        model = model_config.get_agno_model()

        from src.tools.agno_wrappers import get_router_tools

        self._agent = Agent(
            name=self._persona.get("name", "SimpleClaw"),
            model=model,
            instructions=build_agent_instructions(self._persona),
            tools=get_router_tools(),
            storage=self._storage,
            add_history_to_messages=True,
            num_history_runs=5,
            markdown=True,
        )

        self._initialized = True
        logger.info("router.initialized", model=self._settings.router_model_id)

    async def classify_intent(self, message: str) -> Intent:
        """Classify user message intent. Fast pattern matching first, model fallback second."""
        lower = message.lower().strip()

        if lower.startswith("/"):
            return Intent.COMMAND

        status_keywords = ["status", "andamento", "progresso", "fila", "queue"]
        if any(kw in lower for kw in status_keywords):
            return Intent.STATUS

        schedule_keywords = ["agendar", "lembrar", "todo dia", "toda semana", "às", "cron", "alarme"]
        if any(kw in lower for kw in schedule_keywords):
            return Intent.SCHEDULE

        file_keywords = ["gerar pdf", "criar planilha", "fazer documento", "excel", "docx", "relatório"]
        if any(kw in lower for kw in file_keywords):
            return Intent.FILE_REQUEST

        db_keywords = ["banco de dados", "database", "sql", "query", "tabela", "schema"]
        if any(kw in lower for kw in db_keywords):
            return Intent.DB_QUERY

        search_keywords = ["pesquisar", "buscar", "procurar", "search", "encontrar na web"]
        if any(kw in lower for kw in search_keywords):
            return Intent.SEARCH

        task_keywords = [
            "criar", "desenvolver", "implementar", "construir", "fazer",
            "analisar", "processar", "automatizar", "configurar", "montar",
        ]
        if len(lower.split()) > 15 or any(kw in lower for kw in task_keywords):
            return await self._model_classify(message)

        return Intent.CHAT

    async def _model_classify(self, message: str) -> Intent:
        """Use the model for ambiguous intent classification."""
        if not self._agent:
            await self.initialize()

        prompt = (
            "Classifique a intenção desta mensagem em EXATAMENTE uma das categorias: "
            "chat, task, status, schedule, file_request, db_query, search.\n"
            "Responda APENAS com a categoria, sem explicação.\n\n"
            f"Mensagem: {message}"
        )

        try:
            response = self._agent.run(prompt)
            content = response.content.strip().lower() if hasattr(response, "content") else ""

            for intent in Intent:
                if intent.value in content:
                    return intent

            return Intent.TASK
        except Exception as e:
            logger.error("router.classify_failed", error=str(e))
            return Intent.CHAT

    async def chat(self, message: str, user_id: str, session_id: str) -> str:
        """Handle direct chat (no specialist needed)."""
        if not self._agent:
            await self.initialize()

        try:
            response = self._agent.run(
                message,
                user_id=user_id,
                session_id=session_id,
            )
            return response.content if hasattr(response, "content") else str(response)
        except Exception as e:
            logger.error("router.chat_failed", error=str(e), user_id=user_id)
            return "Desculpe, tive um problema ao processar sua mensagem. Tente novamente."

    async def generate_task_spec(self, message: str, user_id: str) -> dict:
        """Generate a technical specification for specialist delegation."""
        if not self._agent:
            await self.initialize()

        prompt = (
            "Gere uma especificação técnica para esta tarefa. "
            "Responda em formato estruturado com:\n"
            "- titulo: (título curto)\n"
            "- descricao: (o que fazer)\n"
            "- agentes: (quais especialistas precisam: db_architect, code_wizard, devops_engineer, research_analyst)\n"
            "- etapas: (lista de etapas ordenadas)\n"
            "- entregaveis: (o que entregar ao usuário)\n"
            "- estimativa: (tempo estimado)\n\n"
            f"Tarefa solicitada: {message}"
        )

        try:
            response = self._agent.run(prompt, user_id=user_id)
            content = response.content if hasattr(response, "content") else str(response)

            return {
                "raw_spec": content,
                "original_request": message,
                "user_id": user_id,
            }
        except Exception as e:
            logger.error("router.spec_failed", error=str(e))
            return {
                "raw_spec": f"Tarefa: {message}",
                "original_request": message,
                "user_id": user_id,
                "error": str(e),
            }

    async def format_status_response(self, tasks: list[dict], user_id: str) -> str:
        """Format task status for the user."""
        if not tasks:
            return "Nenhuma tarefa na fila no momento. 🟢"

        lines = ["📋 *Status das Tarefas:*\n"]
        status_emoji = {
            "pending": "⏳", "processing": "🔄",
            "completed": "✅", "failed": "❌",
            "paused": "⏸️", "cancelled": "🚫",
        }

        for task in tasks:
            emoji = status_emoji.get(task.get("status", ""), "❓")
            lines.append(f"{emoji} *{task.get('title', 'Sem título')}*")
            lines.append(f"   Status: {task.get('status', 'desconhecido')}")
            if task.get("started_at"):
                lines.append(f"   Início: {task['started_at']}")
            lines.append("")

        return "\n".join(lines)
