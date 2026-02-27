"""
SimpleClaw v2.0 - Tool Validator
==================================
Middleware que intercepta erros de tool calls inválidos.

Quando o modelo alucina uma tool que não existe, o provider (Groq, OpenAI)
rejeita com erro. Este módulo:
1. Detecta o padrão do erro
2. Classifica a causa (alucinação, typo, tool deprecada)
3. Tenta recuperar (re-run sem tool call, sugerir tool correta)
4. Se não recuperar, escala com dignidade

Uso:
    Envolve qualquer chamada a agent.run() ou team.run():

    result = await safe_agent_run(agent, message, user_id=..., session_id=...)
"""

from __future__ import annotations

import re
from difflib import get_close_matches
from typing import Any, Optional

import structlog

logger = structlog.get_logger()


class ToolCallError:
    """Parsed representation of a tool call validation error."""

    def __init__(
        self,
        tool_name: str,
        error_message: str,
        failed_generation: str = "",
    ):
        self.tool_name = tool_name
        self.error_message = error_message
        self.failed_generation = failed_generation
        self.cause: str = "unknown"  # hallucination, typo, deprecated, missing
        self.suggested_fix: Optional[str] = None


def parse_tool_error(error: Exception) -> Optional[ToolCallError]:
    """
    Detect if an exception is a tool call validation error.
    Works with Groq, OpenAI, and Anthropic error formats.

    Returns ToolCallError if detected, None otherwise.
    """
    msg = str(error)

    # Groq format: "attempted to call tool 'xxx' which was not in request.tools"
    match = re.search(r"attempted to call tool '(\w+)'", msg)
    if match:
        return ToolCallError(
            tool_name=match.group(1),
            error_message=msg,
            failed_generation=_extract_failed_generation(msg),
        )

    # OpenAI format: "function 'xxx' is not defined"
    match = re.search(r"function '(\w+)' is not defined", msg)
    if match:
        return ToolCallError(
            tool_name=match.group(1),
            error_message=msg,
        )

    # Generic: "tool_use_failed" + tool name
    if "tool_use_failed" in msg:
        match = re.search(r"tool[_\s]+['\"]?(\w+)['\"]?", msg)
        if match:
            return ToolCallError(
                tool_name=match.group(1),
                error_message=msg,
            )

    return None


def _extract_failed_generation(error_msg: str) -> str:
    """Extract the failed_generation content from error message."""
    match = re.search(r"failed_generation['\"]?\s*:\s*['\"](.+?)['\"]", error_msg, re.DOTALL)
    return match.group(1) if match else ""


def classify_tool_error(
    error: ToolCallError,
    available_tools: list[str],
) -> ToolCallError:
    """
    Classify why the tool call failed.

    Checks:
    - Is it a close match to an existing tool? (typo)
    - Is the intent clear from the name? (hallucination with recoverable intent)
    - Is it completely unrelated? (pure hallucination)
    """
    tool_name = error.tool_name.lower()

    # Check for typo / close match
    close = get_close_matches(tool_name, [t.lower() for t in available_tools], n=1, cutoff=0.6)
    if close:
        error.cause = "typo"
        error.suggested_fix = close[0]
        return error

    # Check for hallucination with recognizable intent
    intent_patterns = {
        "schedule": "O modelo tentou agendar algo, mas essa funcionalidade ainda não existe.",
        "email": "O modelo tentou enviar email, mas essa funcionalidade ainda não existe.",
        "notify": "O modelo tentou enviar notificação, mas essa funcionalidade ainda não existe.",
        "download": "O modelo tentou fazer download, mas essa funcionalidade ainda não existe.",
        "upload": "O modelo tentou fazer upload, mas essa funcionalidade ainda não existe.",
        "calendar": "O modelo tentou acessar calendário, mas essa funcionalidade ainda não existe.",
        "reminder": "O modelo tentou criar lembrete, mas essa funcionalidade ainda não existe.",
        "send": "O modelo tentou enviar algo, mas essa funcionalidade ainda não existe.",
        "create_table": "O modelo tentou criar tabela diretamente. Use run_sql com CREATE TABLE.",
        "create_db": "O modelo tentou criar banco diretamente. Use run_sql com CREATE DATABASE.",
    }

    for pattern, explanation in intent_patterns.items():
        if pattern in tool_name:
            error.cause = "hallucination"
            error.suggested_fix = explanation
            return error

    # Pure hallucination - no recognizable intent
    error.cause = "hallucination"
    error.suggested_fix = "O modelo inventou uma ferramenta que não existe."
    return error


def get_available_tool_names(agent) -> list[str]:
    """Extract tool names from an Agno agent or team."""
    names = []
    try:
        tools = getattr(agent, "tools", []) or []
        for tool in tools:
            if callable(tool):
                names.append(tool.__name__)
            elif hasattr(tool, "name"):
                names.append(tool.name)
    except Exception:
        pass
    return names


def build_recovery_prompt(
    original_message: str,
    error: ToolCallError,
    available_tools: list[str],
) -> str:
    """
    Build a prompt that guides the model to respond without hallucinating tools.
    """
    tools_list = ", ".join(available_tools) if available_tools else "nenhuma"

    return (
        f"{original_message}\n\n"
        f"IMPORTANTE: Responda usando APENAS texto ou as ferramentas disponíveis: {tools_list}. "
        f"NÃO tente chamar '{error.tool_name}' ou qualquer outra ferramenta que não esteja na lista. "
        f"Se não conseguir realizar a ação com as ferramentas disponíveis, explique o que pode fazer "
        f"e sugira alternativas."
    )


async def safe_agent_run(
    agent,
    message: str,
    max_tool_retries: int = 2,
    **kwargs,
) -> str:
    """
    Execute agent.run() with tool validation middleware.

    If the model hallucinates a tool:
    1. Parse and classify the error
    2. Retry with corrective prompt (up to max_tool_retries)
    3. If all retries fail, return user-friendly explanation

    Args:
        agent: Agno Agent or Team instance
        message: User message
        max_tool_retries: Max attempts to recover from tool errors
        **kwargs: Passed to agent.run() (user_id, session_id, etc)

    Returns:
        Response content string
    """
    available_tools = get_available_tool_names(agent)
    current_message = message

    for attempt in range(1, max_tool_retries + 1):
        try:
            response = agent.run(current_message, **kwargs)
            content = response.content if hasattr(response, "content") else str(response)
            return content

        except Exception as e:
            tool_error = parse_tool_error(e)

            if tool_error is None:
                # Not a tool error — re-raise for outer error boundary
                raise

            # Classify the error
            tool_error = classify_tool_error(tool_error, available_tools)

            logger.warning(
                "tool_validator.intercepted",
                tool_name=tool_error.tool_name,
                cause=tool_error.cause,
                attempt=attempt,
                suggested_fix=tool_error.suggested_fix,
            )

            if attempt < max_tool_retries:
                # Retry with recovery prompt
                current_message = build_recovery_prompt(
                    message, tool_error, available_tools
                )
                continue

    # All retries exhausted — return dignified response
    logger.error(
        "tool_validator.exhausted",
        tool_name=tool_error.tool_name,
        cause=tool_error.cause,
    )

    if tool_error.cause == "typo" and tool_error.suggested_fix:
        return (
            f"Tentei usar a ferramenta '{tool_error.tool_name}' que não existe, "
            f"mas encontrei '{tool_error.suggested_fix}' que pode ser o que preciso. "
            f"Pode reformular o pedido?"
        )

    if tool_error.suggested_fix and "ainda não existe" in tool_error.suggested_fix:
        return (
            f"Entendi seu pedido, mas a funcionalidade necessária "
            f"({tool_error.tool_name}) ainda não está implementada. "
            f"Está no roadmap e será adicionada em breve. "
            f"Posso ajudar com outra coisa?"
        )

    return (
        "Encontrei uma limitação ao processar seu pedido. "
        "Algumas ações que tentei ainda não estão disponíveis. "
        "Pode reformular de forma diferente ou pedir algo específico?"
    )


async def safe_team_run(
    team,
    message: str,
    max_tool_retries: int = 2,
    **kwargs,
) -> str:
    """
    Same as safe_agent_run but for Team instances.
    Team.run() has the same interface as Agent.run().
    """
    return await safe_agent_run(team, message, max_tool_retries=max_tool_retries, **kwargs)
