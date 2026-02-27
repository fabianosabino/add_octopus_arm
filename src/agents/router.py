"""
SimpleClaw v2.1 - Router Agent
================================
Main orchestrator using Agno framework.
Integrado com Sanity Layer para ground truth e validação de capabilities.
"""

from __future__ import annotations

import uuid
from enum import Enum
from typing import Optional

import structlog
from agno.agent import Agent
from agno.db.postgres import PostgresDb

from src.config.settings import get_settings
from src.config.persona_loader import load_persona, build_agent_instructions

logger = structlog.get_logger()


class Intent(str, Enum):
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
    Integrado com HonestyEnforcer para prevenir confabulação.
    """

    def __init__(self):
        self._settings = get_settings()
        self._persona = load_persona("router")
        self._agent: Optional[Agent] = None
        self._db: Optional[PostgresDb] = None
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return

        self._db = PostgresDb(db_url=self._settings.database_url)

        model_config = self._settings.get_router_model_config()
        model = model_config.get_agno_model()

        from src.tools.agno_wrappers import get_router_tools

        # Build instructions with honesty grounding from manifest
        base_instructions = build_agent_instructions(self._persona)
        try:
            from src.sanity.sanity_layer import HonestyEnforcer
            enforcer = HonestyEnforcer()
            honesty_prompt = enforcer.get_identity_prompt()
            if isinstance(base_instructions, list):
                instructions = [honesty_prompt] + base_instructions
            else:
                instructions = f"{honesty_prompt}\n\n{base_instructions}"
        except Exception:
            instructions = base_instructions

        self._agent = Agent(
            name=self._persona.get("name", "SimpleClaw"),
            model=model,
            instructions=instructions,
            tools=get_router_tools(),
            db=self._db,
            add_history_to_context=True,
            num_history_runs=5,
            markdown=True,
        )

        self._initialized = True
        logger.info("router.initialized", model=self._settings.router_model_id)

    async def classify_intent(self, message: str) -> Intent:
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
        if not self._agent:
            await self.initialize()

        prompt = (
            "Classifique a intenção desta mensagem em EXATAMENTE uma das categorias: "
            "chat, task, status, schedule, file_request, db_query, search.\n"
            "Responda APENAS com a categoria, sem explicação.\n\n"
            f"Mensagem: {message}"
        )

        try:
            from src.tools.tool_validator import safe_agent_run
            content = await safe_agent_run(self._agent, prompt)
            content = content.strip().lower()

            for intent in Intent:
                if intent.value in content:
                    return intent

            return Intent.TASK
        except Exception as e:
            logger.error("router.classify_failed", error=str(e))
            return Intent.CHAT

    async def chat(self, message: str, user_id: str, session_id: str) -> str:
        if not self._agent:
            await self.initialize()

        # Validate intent against capabilities before executing
        try:
            from src.sanity.sanity_layer import validate_intent_against_capabilities
            intent = await self.classify_intent(message)
            decision = validate_intent_against_capabilities(intent.value, message)

            if decision.action == "impossible":
                return f"Entendi seu pedido, mas {decision.reason} Posso ajudar com outra coisa?"
        except ImportError:
            pass

        from src.tools.tool_validator import safe_agent_run

        try:
            return await safe_agent_run(
                self._agent, message,
                user_id=user_id, session_id=session_id,
            )
        except Exception as e:
            logger.error("router.chat_failed", error=str(e), user_id=user_id)
            return "Desculpe, tive um problema ao processar sua mensagem. Tente novamente."

    async def generate_task_spec(self, message: str, user_id: str) -> dict:
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
            from src.tools.tool_validator import safe_agent_run
            content = await safe_agent_run(self._agent, prompt, user_id=user_id)

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
