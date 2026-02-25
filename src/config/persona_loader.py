"""
SimpleClaw v2.0 - Persona Loader
==================================
Loads persona definitions from YAML and creates Agno-compatible configurations.
Supports whitelabel customization via YAML changes only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml
import structlog

from src.config.settings import PERSONAS_DIR

logger = structlog.get_logger()

_personas_cache: dict[str, dict] = {}


def load_persona(name: str, personas_dir: Optional[Path] = None) -> dict[str, Any]:
    """
    Load a persona definition from YAML.
    
    Args:
        name: Persona filename without extension (e.g., 'router', 'db_architect')
        personas_dir: Custom directory for personas (default: config/personas/)
    
    Returns:
        Dict with persona configuration
    """
    if name in _personas_cache:
        return _personas_cache[name]

    dir_path = personas_dir or PERSONAS_DIR
    filepath = dir_path / f"{name}.yaml"

    if not filepath.exists():
        raise FileNotFoundError(f"Persona file not found: {filepath}")

    with open(filepath, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    _personas_cache[name] = data
    logger.info("persona.loaded", name=name, role=data.get("role"))
    return data


def load_all_personas(personas_dir: Optional[Path] = None) -> dict[str, dict]:
    """Load all persona files from the personas directory."""
    dir_path = personas_dir or PERSONAS_DIR
    personas = {}

    for filepath in dir_path.glob("*.yaml"):
        name = filepath.stem
        try:
            personas[name] = load_persona(name, dir_path)
        except Exception as e:
            logger.error("persona.load_failed", name=name, error=str(e))

    return personas


def build_agent_instructions(persona: dict) -> list[str]:
    """Convert persona config into Agno agent instructions list."""
    instructions = []

    if persona.get("role"):
        instructions.append(f"Você é {persona['name']}, {persona['role']}.")

    if persona.get("description"):
        instructions.append(persona["description"])

    if persona.get("tone"):
        instructions.append(f"Tom de comunicação: {persona['tone']}.")

    if persona.get("instructions"):
        instructions.extend(persona["instructions"])

    return instructions


def build_agent_description(persona: dict) -> str:
    """Build a concise agent description for Agno."""
    parts = [persona.get("name", "Agent")]
    if persona.get("role"):
        parts.append(f"- {persona['role']}")
    if persona.get("description"):
        desc = persona["description"]
        if len(desc) > 150:
            desc = desc[:147] + "..."
        parts.append(f"({desc})")
    return " ".join(parts)


def clear_cache():
    """Clear the personas cache (useful for testing or hot-reload)."""
    global _personas_cache
    _personas_cache = {}
