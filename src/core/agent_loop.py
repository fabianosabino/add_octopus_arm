"""
SimpleClaw v3.0 - Agent Loop
===============================
O coração do sistema. Inspirado no pi-agent-core do OpenClaw.

Loop:
    1. Monta contexto (system prompt + histórico + tools)
    2. Manda pro LLM
    3. LLM retornou tool calls? → Executa → Alimenta resultado → Volta pro 2
    4. LLM retornou texto puro? → Resposta final → Sai do loop

Sem Agno. Sem framework. Fala direto com a API.
As tools são funções Python reais que executam coisas reais.
A honestidade é consequência da arquitetura, não módulo bolt-on.
"""

from __future__ import annotations

import json
import traceback
from typing import Any, AsyncGenerator, Callable, Optional

import structlog

from src.core.llm_client import LLMClient, LLMError
from src.core.tool_registry import ToolRegistry
from src.core.session_store import SessionStore

logger = structlog.get_logger()

MAX_TOOL_ROUNDS = 10  # Safety: max tool execution rounds per turn


class LoopEvent:
    """Event emitted during agent loop execution."""

    def __init__(self, event_type: str, data: dict = None):
        self.type = event_type  # turn_start, tool_start, tool_end, response, error
        self.data = data or {}

    def __repr__(self):
        return f"LoopEvent({self.type}, {self.data})"


class AgentLoop:
    """
    The agent execution loop.

    Stateless per call — session state comes from SessionStore.
    Tools come from ToolRegistry.
    LLM access comes from LLMClient.

    Usage:
        loop = AgentLoop(system_prompt="Você é SimpleClaw...")
        result = await loop.run("Crie uma planilha de vendas", user_id="123")
    """

    def __init__(
        self,
        system_prompt: str = "",
        llm_client: Optional[LLMClient] = None,
        tool_registry: Optional[ToolRegistry] = None,
        session_store: Optional[SessionStore] = None,
        on_event: Optional[Callable[[LoopEvent], Any]] = None,
    ):
        self._system_prompt = system_prompt
        self._llm = llm_client or LLMClient()
        self._tools = tool_registry or ToolRegistry()
        self._sessions = session_store or SessionStore()
        self._on_event = on_event

    def _emit(self, event_type: str, data: dict = None) -> None:
        """Emit event for debug window / logging."""
        event = LoopEvent(event_type, data or {})
        logger.debug("agent_loop.event", type=event_type, data=data)
        if self._on_event:
            try:
                self._on_event(event)
            except Exception:
                pass

    async def run(
        self,
        user_message: str,
        user_id: str,
        session_id: str = "main",
        max_rounds: int = MAX_TOOL_ROUNDS,
    ) -> str:
        """
        Execute one full turn of the agent loop.

        1. Load session history
        2. Add user message
        3. Loop: LLM → tool calls → execute → feed back
        4. Return final text response

        Args:
            user_message: What the user said
            user_id: User identifier
            session_id: Session identifier
            max_rounds: Safety limit for tool execution rounds

        Returns:
            Final text response from the agent
        """
        self._emit("turn_start", {"user_message": user_message[:200]})

        # ── 1. Build context ──
        history = self._sessions.load(user_id, session_id)

        messages = []

        # System prompt
        if self._system_prompt:
            messages.append({"role": "system", "content": self._system_prompt})

        # Session history
        messages.extend(history)

        # Current user message
        user_msg = {"role": "user", "content": user_message}
        messages.append(user_msg)
        self._sessions.append(user_id, user_msg, session_id)

        # Tool schemas for the API
        tool_schemas = self._tools.get_schemas_for_api() if self._tools.get_tool_names() else None

        # ── 2. Agent loop ──
        for round_num in range(1, max_rounds + 1):
            try:
                response = await self._llm.chat(
                    messages=messages,
                    tools=tool_schemas,
                )
            except LLMError as e:
                error_msg = f"Erro de comunicação com o modelo: {str(e)[:200]}"
                self._emit("error", {"error": str(e)[:200], "round": round_num})
                self._sessions.append(user_id, {"role": "assistant", "content": error_msg}, session_id)
                return error_msg

            # ── 3. Check: tool calls or final response? ──
            if response.is_final:
                # No tool calls — this is the final answer
                final_text = response.content or "Tarefa concluída."

                # Sanity check (pre-send, not post-correct)
                final_text = self._sanity_check(final_text)

                self._emit("response", {"content": final_text[:200]})
                self._sessions.append(user_id, {"role": "assistant", "content": final_text}, session_id)
                return final_text

            # ── 4. Execute tool calls ──
            # Add assistant message with tool calls to context
            assistant_msg = {
                "role": "assistant",
                "content": response.content or "",
                "tool_calls": [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc["arguments"], ensure_ascii=False),
                        },
                    }
                    for tc in response.tool_calls
                ],
            }
            messages.append(assistant_msg)
            self._sessions.append(user_id, assistant_msg, session_id)

            for tc in response.tool_calls:
                tool_name = tc["name"]
                tool_args = tc["arguments"]
                tool_id = tc["id"]

                self._emit("tool_start", {"name": tool_name, "args": tool_args, "round": round_num})

                # Validate against capability registry BEFORE execution
                if not self._tools.has_tool(tool_name):
                    result = self._handle_unknown_tool(tool_name)
                else:
                    result = self._tools.execute(tool_name, tool_args)

                self._emit("tool_end", {"name": tool_name, "result": result[:200], "round": round_num})

                # Add tool result to context
                tool_result_msg = {
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "name": tool_name,
                    "content": result,
                }
                messages.append(tool_result_msg)
                self._sessions.append(user_id, tool_result_msg, session_id)

            # Continue loop — LLM will see tool results and decide next action

        # Safety: max rounds exceeded
        timeout_msg = (
            "⚠️ Atingi o limite de execuções para esta tarefa. "
            "Resultados parciais podem estar disponíveis. "
            "Reformule o pedido de forma mais específica se necessário."
        )
        self._sessions.append(user_id, {"role": "assistant", "content": timeout_msg}, session_id)
        return timeout_msg

    def _handle_unknown_tool(self, tool_name: str) -> str:
        """Handle hallucinated tool call using CapabilityRegistry."""
        try:
            from src.sanity.sanity_layer import CapabilityRegistry
            registry = CapabilityRegistry()
            validation = registry.validate_tool_call(tool_name)
            reason = validation.get("suggestion") or validation.get("reason", "")
            available = ", ".join(registry.available_tool_names)
            return (
                f"ERRO: Tool '{tool_name}' não existe. {reason} "
                f"Tools disponíveis: {available}. "
                f"Use apenas tools da lista."
            )
        except Exception:
            available = ", ".join(self._tools.get_tool_names())
            return (
                f"ERRO: Tool '{tool_name}' não existe. "
                f"Tools disponíveis: {available}. "
                f"Use apenas tools da lista."
            )

    def _sanity_check(self, text: str) -> str:
        """Light sanity check on final response."""
        try:
            from src.sanity.sanity_layer import HonestyEnforcer
            enforcer = HonestyEnforcer()
            result = enforcer.check_response(text)
            if not result["honest"]:
                logger.warning("agent_loop.honesty_violation", violations=result["violations"])
                return result["corrected"]
        except ImportError:
            pass
        return text


def build_system_prompt() -> str:
    """
    Build the system prompt from manifest + persona.
    This is the SOUL of the agent.
    """
    parts = []

    # Identity from manifest
    try:
        from src.sanity.frozen_manifest import FrozenManifest
        manifest = FrozenManifest()
        identity = manifest.identity
        parts.append(
            f"Você é {identity['name']} v{identity['version']}. "
            f"{identity.get('description', '')}."
        )
    except Exception:
        parts.append("Você é SimpleClaw, um assistente pessoal multi-agente.")

    # Core behavior rules
    parts.append("""
REGRAS DE EXECUÇÃO:
1. EXECUTE tarefas usando as tools disponíveis. Não DESCREVA o que faria.
2. Se o usuário pede algo que requer uma tool, CHAME a tool.
3. Se não existe tool para o pedido, diga "Não posso fazer X. Posso fazer Y ou Z."
4. NUNCA gere código Python em resposta — use execute_python se precisar rodar código.
5. NUNCA invente tools que não existem.
6. NUNCA invente sua versão, modelo, ou capacidades.
7. Se não sabe, diga "não sei". Se não pode, diga "não posso".

REGRAS DE COMUNICAÇÃO:
- Responda em português brasileiro
- Seja conciso. Não repita informações.
- Quando uma tool retornar sucesso, relate o resultado sem rodeios.
""".strip())

    # Persona (if available)
    try:
        from src.config.persona_loader import load_persona
        persona = load_persona("router")
        if persona.get("personality"):
            parts.append(f"Personalidade: {persona['personality']}")
    except Exception:
        pass

    return "\n\n".join(parts)
