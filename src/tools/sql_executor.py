"""
SimpleClaw v2.0 - SQL Executor Tool
=====================================
Executes SQL queries safely on internal and external databases.
Enforces permission levels and prevents destructive operations without approval.
"""

from __future__ import annotations

import re
from typing import Optional

import structlog
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from src.config.settings import get_settings
from src.tools.vault import Vault

logger = structlog.get_logger()

# Statements that require explicit user confirmation
DESTRUCTIVE_PATTERNS = [
    r"\bDROP\b",
    r"\bTRUNCATE\b",
    r"\bDELETE\b(?!.*\bWHERE\b)",  # DELETE without WHERE
    r"\bALTER\b.*\bDROP\b",
    r"\bGRANT\b",
    r"\bREVOKE\b",
]

# Statements blocked entirely on external databases
BLOCKED_ON_EXTERNAL = [
    r"\bDROP\s+DATABASE\b",
    r"\bCREATE\s+DATABASE\b",
    r"\bDROP\s+SCHEMA\b",
]


def _is_destructive(query: str) -> bool:
    """Check if a query contains destructive operations."""
    upper_query = query.upper().strip()
    return any(re.search(p, upper_query) for p in DESTRUCTIVE_PATTERNS)


def _is_blocked_external(query: str) -> bool:
    """Check if a query is blocked on external databases."""
    upper_query = query.upper().strip()
    return any(re.search(p, upper_query) for p in BLOCKED_ON_EXTERNAL)


def _is_read_only(query: str) -> bool:
    """Check if query is read-only."""
    upper_query = query.strip().upper()
    return upper_query.startswith(("SELECT", "EXPLAIN", "SHOW", "DESCRIBE", "\\D"))


async def execute_internal(
    query: str,
    params: Optional[dict] = None,
    confirm_destructive: bool = False,
) -> dict:
    """
    Execute query on the internal SimpleClaw database.

    Args:
        query: SQL query string
        params: Query parameters (for parameterized queries)
        confirm_destructive: User explicitly confirmed destructive operation

    Returns:
        Dict with columns, rows, row_count, or error
    """
    if _is_destructive(query) and not confirm_destructive:
        return {
            "error": "DESTRUCTIVE_QUERY",
            "message": (
                "⚠️ Esta query contém operações destrutivas. "
                "Confirme explicitamente para executar."
            ),
            "query": query,
        }

    settings = get_settings()

    try:
        engine = create_engine(settings.database_url, echo=False)
        with engine.connect() as conn:
            result = conn.execute(text(query), params or {})

            if result.returns_rows:
                columns = list(result.keys())
                rows = [dict(zip(columns, row)) for row in result.fetchall()]
                conn.commit()
                return {
                    "columns": columns,
                    "rows": rows,
                    "row_count": len(rows),
                }
            else:
                row_count = result.rowcount
                conn.commit()
                return {
                    "message": f"Query executada com sucesso. {row_count} linha(s) afetada(s).",
                    "row_count": row_count,
                }

    except SQLAlchemyError as e:
        logger.error("sql.internal_error", query=query[:100], error=str(e))
        return {"error": "SQL_ERROR", "message": str(e)}
    finally:
        engine.dispose()


async def execute_external(
    connection_string: str,
    query: str,
    params: Optional[dict] = None,
    user_id: Optional[str] = None,
    confirm_destructive: bool = False,
) -> dict:
    """
    Execute query on an external database.

    Extra safety: blocks database-level operations entirely,
    requires confirmation for any DML.

    Args:
        connection_string: SQLAlchemy connection URL or vault key name
        query: SQL query string
        params: Query parameters
        user_id: User ID for vault lookup
        confirm_destructive: User confirmed destructive operation

    Returns:
        Dict with columns, rows, row_count, or error
    """
    # Block dangerous operations on external databases
    if _is_blocked_external(query):
        return {
            "error": "BLOCKED",
            "message": "❌ Operações de DROP/CREATE DATABASE não são permitidas em bancos externos.",
        }

    # Non-SELECT queries require confirmation
    if not _is_read_only(query) and not confirm_destructive:
        return {
            "error": "CONFIRM_REQUIRED",
            "message": (
                "⚠️ Esta query modifica dados em um banco externo. "
                "Confirme explicitamente para executar."
            ),
            "query": query,
        }

    # Resolve connection string from vault if it looks like a key name
    actual_conn_string = connection_string
    if not connection_string.startswith(("postgresql", "mysql", "sqlite", "mssql")):
        try:
            import uuid
            vault = Vault()
            resolved = await vault.retrieve(
                connection_string,
                user_id=uuid.UUID(user_id) if user_id else None,
            )
            if resolved:
                actual_conn_string = resolved
            else:
                return {
                    "error": "VAULT_NOT_FOUND",
                    "message": f"Credencial '{connection_string}' não encontrada no vault.",
                }
        except Exception as e:
            return {"error": "VAULT_ERROR", "message": str(e)}

    try:
        engine = create_engine(actual_conn_string, echo=False)
        with engine.connect() as conn:
            result = conn.execute(text(query), params or {})

            if result.returns_rows:
                columns = list(result.keys())
                rows = [dict(zip(columns, row)) for row in result.fetchall()]
                conn.commit()

                logger.info(
                    "sql.external_executed",
                    read_only=True,
                    row_count=len(rows),
                )
                return {
                    "columns": columns,
                    "rows": rows,
                    "row_count": len(rows),
                }
            else:
                row_count = result.rowcount
                conn.commit()
                logger.info(
                    "sql.external_executed",
                    read_only=False,
                    row_count=row_count,
                )
                return {
                    "message": f"Query executada. {row_count} linha(s) afetada(s).",
                    "row_count": row_count,
                }

    except SQLAlchemyError as e:
        logger.error("sql.external_error", query=query[:100], error=str(e))
        return {"error": "SQL_ERROR", "message": str(e)}
    finally:
        engine.dispose()


def format_query_result(result: dict, max_rows: int = 20) -> str:
    """Format query results for Telegram display."""
    if "error" in result:
        return f"⚠️ {result['message']}"

    if "message" in result and "columns" not in result:
        return f"✅ {result['message']}"

    columns = result.get("columns", [])
    rows = result.get("rows", [])
    total = result.get("row_count", len(rows))

    if not rows:
        return "✅ Query executada. Nenhum resultado retornado."

    # Build table-like output
    lines = [f"📊 *{total} resultado(s):*\n"]

    # Header
    header = " | ".join(str(c) for c in columns)
    lines.append(f"`{header}`")
    lines.append(f"`{'─' * min(len(header), 60)}`")

    # Rows
    for row in rows[:max_rows]:
        values = " | ".join(str(row.get(c, ""))[:30] for c in columns)
        lines.append(f"`{values}`")

    if total > max_rows:
        lines.append(f"\n_... e mais {total - max_rows} linha(s)_")

    return "\n".join(lines)
