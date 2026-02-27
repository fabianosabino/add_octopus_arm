"""
SimpleClaw v3.0 - Tool Registry
==================================
Converte funções Python em formato OpenAI function calling.
Extrai nome, descrição e parâmetros da docstring e type hints.

As tools existentes (agno_wrappers.py) são funções Python puras.
Este módulo as registra e expõe pro LLM sem depender do Agno.
"""

from __future__ import annotations

import inspect
import json
from typing import Any, Callable, Optional, get_type_hints

import structlog

logger = structlog.get_logger()

# Python type → JSON Schema type
TYPE_MAP = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


class ToolRegistry:
    """
    Registry de tools disponíveis para o agent loop.
    Converte funções Python em OpenAI function calling format.
    """

    def __init__(self):
        self._tools: dict[str, Callable] = {}
        self._schemas: dict[str, dict] = {}

    def register(self, func: Callable) -> None:
        """Register a Python function as a tool."""
        name = func.__name__
        schema = self._function_to_schema(func)
        self._tools[name] = func
        self._schemas[name] = schema
        logger.debug("tool_registry.registered", name=name)

    def register_many(self, funcs: list[Callable]) -> None:
        """Register multiple functions."""
        for func in funcs:
            self.register(func)

    def get_tool_function(self, name: str) -> Optional[Callable]:
        """Get the callable for a tool name."""
        return self._tools.get(name)

    def get_schemas_for_api(self) -> list[dict]:
        """
        Get all tool schemas in OpenAI function calling format.
        Ready to pass as 'tools' parameter to the API.
        """
        return [
            {
                "type": "function",
                "function": schema,
            }
            for schema in self._schemas.values()
        ]

    def get_tool_names(self) -> list[str]:
        """List all registered tool names."""
        return list(self._tools.keys())

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    def execute(self, name: str, arguments: dict) -> str:
        """
        Execute a tool by name with given arguments.
        Returns string result (tools always return strings for LLM context).
        """
        func = self._tools.get(name)
        if func is None:
            return f"Erro: Tool '{name}' não existe. Disponíveis: {', '.join(self.get_tool_names())}"

        try:
            result = func(**arguments)
            return str(result)
        except TypeError as e:
            return f"Erro: Parâmetros inválidos para '{name}': {str(e)}"
        except Exception as e:
            return f"Erro ao executar '{name}': {str(e)}"

    def _function_to_schema(self, func: Callable) -> dict:
        """Convert a Python function to OpenAI function schema."""
        name = func.__name__
        doc = inspect.getdoc(func) or ""

        # Parse description (first line of docstring)
        description = doc.split("\n")[0].strip() if doc else name

        # Get parameters from signature and type hints
        sig = inspect.signature(func)
        try:
            hints = get_type_hints(func)
        except Exception:
            hints = {}

        properties = {}
        required = []

        # Parse Args section from docstring for descriptions
        arg_descriptions = self._parse_arg_descriptions(doc)

        for param_name, param in sig.parameters.items():
            if param_name in ("self", "cls"):
                continue

            # Type
            python_type = hints.get(param_name, str)
            json_type = TYPE_MAP.get(python_type, "string")

            prop: dict[str, Any] = {"type": json_type}

            # Description from docstring
            if param_name in arg_descriptions:
                prop["description"] = arg_descriptions[param_name]

            # Default value
            if param.default is not inspect.Parameter.empty:
                prop["default"] = param.default
            else:
                required.append(param_name)

            properties[param_name] = prop

        schema: dict[str, Any] = {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
            },
        }

        if required:
            schema["parameters"]["required"] = required

        return schema

    def _parse_arg_descriptions(self, docstring: str) -> dict[str, str]:
        """Parse 'Args:' section from Google-style docstring."""
        descriptions = {}
        in_args = False

        for line in docstring.split("\n"):
            stripped = line.strip()

            if stripped.lower().startswith("args:"):
                in_args = True
                continue
            elif stripped.lower().startswith("returns:") or stripped.lower().startswith("raises:"):
                in_args = False
                continue

            if in_args and ":" in stripped:
                parts = stripped.split(":", 1)
                param_name = parts[0].strip()
                param_desc = parts[1].strip()
                if param_name and param_desc:
                    descriptions[param_name] = param_desc

        return descriptions


def build_default_registry() -> ToolRegistry:
    """
    Build registry with all SimpleClaw tools.
    Same tools that were in agno_wrappers, now framework-agnostic.
    """
    from src.tools.agno_wrappers import (
        search_web,
        run_sql,
        create_csv,
        create_xlsx,
        create_pdf,
        create_docx,
        create_chart,
        execute_python,
        git_save,
        git_history,
    )

    registry = ToolRegistry()
    registry.register_many([
        search_web,
        run_sql,
        create_csv,
        create_xlsx,
        create_pdf,
        create_docx,
        create_chart,
        execute_python,
        git_save,
        git_history,
    ])

    # Try to register superset tools
    try:
        from src.tools.superset_manager import (
            superset_query,
            superset_list_databases,
            superset_create_dataset,
            superset_create_dashboard,
            superset_list_dashboards,
        )
        registry.register_many([
            superset_query,
            superset_list_databases,
            superset_create_dataset,
            superset_create_dashboard,
            superset_list_dashboards,
        ])
    except ImportError:
        logger.debug("tool_registry.superset_unavailable")

    logger.info("tool_registry.built", tools=registry.get_tool_names())
    return registry
