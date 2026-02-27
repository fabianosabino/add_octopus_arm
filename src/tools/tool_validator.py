"""
SimpleClaw v2.1 - Tool Validator (Middleware Global)
=====================================================
Intercepta tool calls inválidos ANTES de quebrarem a aplicação.
Agora integrado com o CapabilityRegistry da Sanity Layer.

Quando o modelo alucina uma tool:
1. CapabilityRegistry classifica (registrada, não-disponível, typo, desconhecida)
2. Retry com prompt corretivo listando tools reais
3. Se esgotar retries, retorna mensagem digna

Uso:
    result = await safe_agent_run(agent, message, user_id=..., session_id=...)
"""

from __future__ import annotations

import re
from typing import Optional

import structlog

logger = structlog.get_logger()


class ToolCallError:
    """Parsed representation of a tool call validation error."""

    def __init__(self, tool_name: str, error_message: str, failed_generation: str = ""):
        self.tool_name = tool_name
        self.error_message = error_message
        self.failed_generation = failed_generation


def parse_tool_error(error: Exception) -> Optional[ToolCallError]:
    """
    Detect if an exception is a tool call validation error.
    Works with Groq, OpenAI, and Anthropic error formats.
    """
    msg = str(error)

    # Groq: "attempted to call tool 'xxx' which was not in request.tools"
    match = re.search(r"attempted to call tool '(\w+)'", msg)
    if match:
        return ToolCallError(tool_name=match.group(1), error_message=msg)

    # OpenAI: "function 'xxx' is not defined"
    match = re.search(r"function '(\w+)' is not defined", msg)
    if match:
        return ToolCallError(tool_name=match.group(1), error_message=msg)

    # Generic: "tool_use_failed"
    if "tool_use_failed" in msg:
        match = re.search(r"tool[_\s]+['\"]?(\w+)['\"]?", msg)
        if match:
            return ToolCallError(tool_name=match.group(1), error_message=msg)

    return None


def classify_with_registry(tool_name: str) -> dict:
    """
    Classify a tool call error using the CapabilityRegistry.
    Falls back to fuzzy matching if registry not available.
    """
    try:
        from src.sanity.sanity_layer import CapabilityRegistry
        registry = CapabilityRegistry()
        return registry.validate_tool_call(tool_name)
    except Exception as e:
        logger.warning("tool_validator.registry_unavailable", error=str(e))
        return {
            "valid": False,
            "reason": f"Tool '{tool_name}' não reconhecida.",
            "suggestion": "Responda usando apenas texto.",
            "capability": None,
        }


def build_recovery_prompt(original_message: str, tool_name: str, validation: dict) -> str:
    """Build prompt that guides the model away from hallucinated tools."""
    try:
        from src.sanity.sanity_layer import CapabilityRegistry
        registry = CapabilityRegistry()
        tools_list = ", ".join(registry.available_tool_names)
    except Exception:
        tools_list = "search_web, run_sql, execute_python, create_csv, create_xlsx, create_pdf, create_docx, create_chart, git_save, git_history"

    reason = validation.get("suggestion") or validation.get("reason", "")

    return (
        f"{original_message}\n\n"
        f"IMPORTANTE: A ferramenta '{tool_name}' NÃO existe. {reason}\n"
        f"Ferramentas disponíveis: {tools_list}.\n"
        f"Responda usando APENAS texto ou ferramentas da lista acima. "
        f"Se não conseguir realizar a ação, explique o que pode fazer."
    )


async def safe_agent_run(agent, message: str, max_tool_retries: int = 2, **kwargs) -> str:
    """
    Execute agent.run() com middleware de validação de tools.

    Se o modelo alucinar uma tool:
    1. Parse e classifica via CapabilityRegistry
    2. Retry com prompt corretivo
    3. Se esgotar, retorna mensagem amigável

    Args:
        agent: Agno Agent instance
        message: User message
        max_tool_retries: Max recovery attempts
        **kwargs: Passed to agent.run()

    Returns:
        Response content string
    """
    current_message = message

    for attempt in range(1, max_tool_retries + 1):
        try:
            response = agent.run(current_message, **kwargs)
            content = response.content if hasattr(response, "content") else str(response)

            # Post-process: sanity check honesty
            try:
                from src.sanity.sanity_layer import sanity_check_response
                content = sanity_check_response(content)
            except ImportError:
                pass

            return content

        except Exception as e:
            tool_error = parse_tool_error(e)

            if tool_error is None:
                raise  # Not a tool error — re-raise for outer boundary

            validation = classify_with_registry(tool_error.tool_name)

            logger.warning(
                "tool_validator.intercepted",
                tool_name=tool_error.tool_name,
                valid=validation["valid"],
                reason=validation.get("reason", ""),
                attempt=attempt,
            )

            if attempt < max_tool_retries:
                current_message = build_recovery_prompt(
                    message, tool_error.tool_name, validation
                )
                continue

    # All retries exhausted
    logger.error("tool_validator.exhausted", tool_name=tool_error.tool_name)

    suggestion = validation.get("suggestion", "")
    if suggestion and "ainda não implementado" in suggestion.lower():
        return (
            f"Entendi seu pedido, mas {suggestion.lower()} "
            f"Posso ajudar com outra coisa?"
        )

    if suggestion and "quis dizer" in suggestion.lower():
        return (
            f"Tentei usar uma ferramenta que não existe. {suggestion} "
            f"Pode reformular o pedido?"
        )

    return (
        "Encontrei uma limitação ao processar seu pedido. "
        "Algumas ações que tentei ainda não estão disponíveis. "
        "Pode reformular de forma diferente?"
    )


async def safe_team_run(team, message: str, max_tool_retries: int = 2, **kwargs) -> str:
    """Same as safe_agent_run but for Team instances."""
    return await safe_agent_run(team, message, max_tool_retries=max_tool_retries, **kwargs)
