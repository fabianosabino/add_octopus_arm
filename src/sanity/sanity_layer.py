"""
SimpleClaw v2.1 - Sanity Layer
================================
Camada de validação em código Python puro (Pydantic).
Nunca depende de prompt engineering para segurança.

Componentes:
- SystemManifest: carrega YAML imutável como ground truth
- CapabilityRegistry: valida tools antes de execução
- HonestyEnforcer: impede confabulação de identidade
- IntentValidator: classifica se intent é direta, delegável ou impossível

Regra de ouro: Se inválido → PARA. Nunca propaga. Nunca confabula.
"""

from __future__ import annotations

import re
from difflib import get_close_matches
from pathlib import Path
from typing import Any, Optional

import structlog
import yaml
from pydantic import BaseModel, Field

logger = structlog.get_logger()

# ─── MANIFEST ────────────────────────────────────────────────

_manifest_cache: Optional[dict] = None
MANIFEST_PATH = Path(__file__).parent / "system_manifest.yaml"


def load_manifest(path: Optional[Path] = None) -> dict:
    """Load system manifest (cached, read-only)."""
    global _manifest_cache
    if _manifest_cache is not None:
        return _manifest_cache

    target = path or MANIFEST_PATH
    if not target.exists():
        logger.error("sanity.manifest_not_found", path=str(target))
        raise FileNotFoundError(f"SystemManifest not found: {target}")

    with open(target, "r", encoding="utf-8") as f:
        _manifest_cache = yaml.safe_load(f)

    logger.info("sanity.manifest_loaded", version=_manifest_cache.get("identity", {}).get("version"))
    return _manifest_cache


def get_identity() -> dict:
    """Get system identity from manifest."""
    m = load_manifest()
    return m.get("identity", {})


def get_limits() -> dict:
    """Get system limits from manifest."""
    m = load_manifest()
    return m.get("limits", {})


# ─── CAPABILITY REGISTRY ────────────────────────────────────

class Capability(BaseModel):
    id: str
    description: str
    agent: str = ""
    tool: str = ""


class UnavailableCapability(BaseModel):
    id: str
    reason: str
    planned: bool = False


class CapabilityRegistry:
    """
    Registry de capacidades reais do sistema.
    Carregado do manifest, read-only em runtime.
    """

    def __init__(self):
        m = load_manifest()
        caps = m.get("capabilities", {})

        self._available: dict[str, Capability] = {}
        for c in caps.get("available", []):
            cap = Capability(**c)
            self._available[cap.id] = cap

        self._unavailable: dict[str, UnavailableCapability] = {}
        for c in caps.get("not_available", []):
            ucap = UnavailableCapability(**c)
            self._unavailable[ucap.id] = ucap

        self._tool_to_cap: dict[str, Capability] = {}
        for cap in self._available.values():
            if cap.tool:
                self._tool_to_cap[cap.tool] = cap

    @property
    def available_tool_names(self) -> list[str]:
        return [c.tool for c in self._available.values() if c.tool]

    @property
    def available_capability_ids(self) -> list[str]:
        return list(self._available.keys())

    def is_tool_registered(self, tool_name: str) -> bool:
        """Check if a tool is in the registry."""
        return tool_name in self._tool_to_cap

    def validate_tool_call(self, tool_name: str) -> dict:
        """
        Validate a tool call against the registry.

        Returns:
            {
                "valid": bool,
                "reason": str,         # explanation if invalid
                "suggestion": str,     # suggested alternative
                "capability": dict,    # matched capability if valid
            }
        """
        # Exact match
        if tool_name in self._tool_to_cap:
            cap = self._tool_to_cap[tool_name]
            return {
                "valid": True,
                "reason": "",
                "suggestion": "",
                "capability": cap.model_dump(),
            }

        # Check unavailable list (known non-capabilities)
        # Normalize: schedule_message, schedulemessage, etc
        normalized = tool_name.lower().replace("_", "").replace("-", "")
        for uid, ucap in self._unavailable.items():
            uid_normalized = uid.lower().replace("_", "").replace("-", "")
            if normalized == uid_normalized or normalized in uid_normalized or uid_normalized in normalized:
                planned_msg = " Está no roadmap." if ucap.planned else ""
                return {
                    "valid": False,
                    "reason": ucap.reason,
                    "suggestion": f"{ucap.reason}{planned_msg}",
                    "capability": None,
                }

        # Fuzzy match against available tools
        close = get_close_matches(
            tool_name.lower(),
            [t.lower() for t in self._tool_to_cap.keys()],
            n=1,
            cutoff=0.6,
        )
        if close:
            return {
                "valid": False,
                "reason": f"Tool '{tool_name}' não existe.",
                "suggestion": f"Você quis dizer '{close[0]}'?",
                "capability": None,
            }

        # Completely unknown
        return {
            "valid": False,
            "reason": f"Tool '{tool_name}' não existe no sistema.",
            "suggestion": "Responda usando apenas texto ou as ferramentas registradas.",
            "capability": None,
        }

    def get_tools_for_agent(self, agent_id: str) -> list[str]:
        """Get list of tool names available to a specific agent."""
        return [
            c.tool for c in self._available.values()
            if c.agent == agent_id and c.tool
        ]


# ─── HONESTY ENFORCER ───────────────────────────────────────

class HonestyEnforcer:
    """
    Intercepta respostas que confabulam sobre identidade.
    Consulta o manifest para ground truth.
    """

    def __init__(self):
        self._identity = get_identity()
        m = load_manifest()
        self._agents = {a["id"]: a for a in m.get("agents", [])}
        self._limits = m.get("limits", {})

    def check_response(self, response_text: str) -> dict:
        """
        Verify if response contains identity confabulation.

        Returns:
            {
                "honest": bool,
                "violations": list[str],
                "corrected": str,  # corrected response if dishonest
            }
        """
        violations = []
        corrected = response_text

        # Check for fake version claims
        version_pattern = r'vers[aã]o\s+([\d]+\.[\d]+[.\d]*)'
        for match in re.finditer(version_pattern, response_text.lower()):
            claimed = match.group(1)
            real = self._identity.get("version", "")
            if claimed != real:
                violations.append(
                    f"Afirmou ser versão {claimed}, real é {real}"
                )
                corrected = corrected.replace(match.group(0), f"versão {real}")

        # Check for fake model claims
        fake_model_patterns = [
            r'(llama[\s-]*\d+b)', r'(gpt[\s-]*\d)', r'(claude[\s-]*\d)',
            r'(gemini)', r'(mistral[\s-]*\d+)',
        ]
        for pattern in fake_model_patterns:
            match = re.search(pattern, response_text.lower())
            if match:
                violations.append(
                    f"Mencionou modelo '{match.group(1)}' sem ground truth"
                )

        # Check for fake specialist claims
        fake_specialist_patterns = [
            r'especialista\s+em\s+(multimídia|multimedia|video|imagem)',
            r'agente\s+de\s+(deploy|deployment)',
            r'módulo\s+de\s+(email|notificação)',
        ]
        for pattern in fake_specialist_patterns:
            match = re.search(pattern, response_text.lower())
            if match:
                violations.append(
                    f"Inventou capacidade '{match.group(1)}' que não existe"
                )

        return {
            "honest": len(violations) == 0,
            "violations": violations,
            "corrected": corrected,
        }

    def get_identity_prompt(self) -> str:
        """
        Generate identity grounding to inject in agent instructions.
        Prevents the model from inventing facts about itself.
        """
        name = self._identity.get("name", "SimpleClaw")
        version = self._identity.get("version", "unknown")
        desc = self._identity.get("description", "")

        agents_desc = []
        for aid, a in self._agents.items():
            agents_desc.append(f"- {a['name']} ({a['role']})")

        return (
            f"Você é {name} v{version}. {desc}.\n"
            f"Sua equipe real:\n" + "\n".join(agents_desc) + "\n\n"
            f"REGRAS DE HONESTIDADE:\n"
            f"- NUNCA invente sua versão, modelo, ou capacidades.\n"
            f"- NUNCA afirme ter agentes ou tools que não existem.\n"
            f"- Se não sabe, diga 'não sei'.\n"
            f"- Se não consegue fazer, diga 'não posso fazer isso ainda'.\n"
            f"- Informações sobre seu modelo vêm do .env, não invente nomes de modelo.\n"
        )


# ─── INTENT VALIDATOR ───────────────────────────────────────

class IntentDecision(BaseModel):
    """Result of intent validation against capabilities."""
    action: str = Field(description="'direct', 'delegate', 'impossible'")
    reason: str = ""
    agent: str = ""
    capability_id: str = ""


def validate_intent_against_capabilities(intent: str, message: str) -> IntentDecision:
    """
    Cross-check classified intent against real capabilities.

    Returns whether the intent can be handled directly,
    needs delegation, or is impossible.
    """
    registry = CapabilityRegistry()

    # Direct intents (router handles)
    if intent in ("chat", "status", "command"):
        return IntentDecision(action="direct", reason="Router handles directly")

    # Search
    if intent == "search":
        if registry.is_tool_registered("search_web"):
            return IntentDecision(
                action="direct",
                capability_id="search_web",
                agent="router",
            )
        return IntentDecision(action="impossible", reason="Pesquisa não disponível")

    # File generation
    if intent == "file_request":
        return IntentDecision(
            action="delegate",
            agent="code_wizard",
            reason="Geração de arquivos requer specialist",
        )

    # DB queries
    if intent == "db_query":
        if registry.is_tool_registered("run_sql"):
            return IntentDecision(
                action="delegate",
                agent="db_architect",
                capability_id="run_sql",
            )
        return IntentDecision(action="impossible", reason="SQL não disponível")

    # Task (complex)
    if intent == "task":
        return IntentDecision(
            action="delegate",
            reason="Tarefa complexa requer equipe especialista",
        )

    # Schedule
    if intent == "schedule":
        validation = registry.validate_tool_call("schedule_message")
        if not validation["valid"]:
            return IntentDecision(
                action="impossible",
                reason=validation["suggestion"] or validation["reason"],
            )

    return IntentDecision(action="direct", reason="Fallback to router")


# ─── COMPOSITE: SANITY CHECK ────────────────────────────────

def sanity_check_response(response_text: str) -> str:
    """
    Run full sanity check on an agent response before sending to user.
    Returns corrected text if needed.
    """
    enforcer = HonestyEnforcer()
    result = enforcer.check_response(response_text)

    if not result["honest"]:
        logger.warning(
            "sanity.honesty_violation",
            violations=result["violations"],
        )
        return result["corrected"]

    return response_text
