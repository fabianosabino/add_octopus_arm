"""
SimpleClaw v3.0 - SQL Executor Tool
=====================================
Executes SQL queries safely with database isolation:
  - simpleclaw (system): READ-ONLY for the model. System tables only.
  - simpleclaw_data (userdata): FULL ACCESS for the model. User's data.
  - external: Via vault credentials. Extra safety.
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

# ─── SAFETY PATTERNS ────────────────────────────────────────

DESTRUCTIVE_PATTERNS = [
    r"\bDROP\b",
    r"\bTRUNCATE\b",
    r"\bDELETE\b(?!.*\bWHERE\b)",  # DELETE without WHERE
    r"\bALTER\b.*\bDROP\b",
    r"\bGRANT\b",
    r"\bREVOKE\b",
]

BLOCKED_ON_EXTERNAL = [
    r"\bDROP\s+DATABASE\b",
    r"\bCREATE\s+DATABASE\b",
    r"\bDROP\s+SCHEMA\b",
]

# Queries that MODIFY data (anything that's not read-only)
WRITE_PATTERNS = [
    r"\bINSERT\b",
    r"\bUPDATE\b",
    r"\bDELETE\b",
    r"\bCREATE\b",
    r"\bDROP\b",
    r"\bALTER\b",
    r"\bTRUNCATE\b",
    r"\bGRANT\b",
    r"\bREVOKE\b",
]


def _is_destructive(query: str) -> bool:
    upper_query = query.upper().strip()
    return any(re.search(p, upper_query) for p in DESTRUCTIVE_PATTERNS)


def _is_blocked_external(query: str) -> bool:
    upper_query = query.upper().strip()
    return any(re.search(p, upper_query) for p in BLOCKED_ON_EXTERNAL)


def _is_read_only(query: str) -> bool:
    upper_query = query.strip().upper()
    return upper_query.startswith(("SELECT", "EXPLAIN", "SHOW", "DESCRIBE", "\\D"))


def _is_write(query: str) -> bool:
    upper_query = query.upper().strip()
    return any(re.search(p, upper_query) for p in WRITE_PATTERNS)


def _get_userdata_url() -> str:
    """Build connection URL for the userdata database."""
    settings = get_settings()
    base_url = settings.database_url
    # Replace database name: simpleclaw -> simpleclaw_data
    # Handles both postgresql+psycopg://.../ and postgresql://.../ formats
    if "/simpleclaw" in base_url:
        # Replace last occurrence of /simpleclaw with /simpleclaw_data
        parts = base_url.rsplit("/simpleclaw", 1)
        return parts[0] + "/simpleclaw_data" + (parts[1] if len(parts) > 1 and parts[1] else "")
    return base_url  # Fallback: use same DB


# ─── INTERNAL (system) — READ ONLY for model ────────────────

async def execute_internal(
    query: str,
    params: Optional[dict] = None,
    confirm_destructive: bool = False,
) -> dict:
    """
    Execute query on the SYSTEM database (simpleclaw).
    READ-ONLY for the model. Write operations are blocked.
    Only system internals (watchdog, scheduler) bypass this.
    """
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


# ─── USERDATA — model has full access ────────────────────────

async def execute_userdata(
    query: str,
    params: Optional[dict] = None,
    confirm_destructive: bool = False,
) -> dict:
    """
    Execute query on the USERDATA database (simpleclaw_data).
    The model can CREATE, INSERT, UPDATE, DELETE here.
    Destructive operations (DROP, TRUNCATE) still require confirmation.
    """
    if _is_destructive(query) and not confirm_destructive:
        return {
            "error": "DESTRUCTIVE_QUERY",
            "message": (
                "⚠️ Esta query contém operações destrutivas (DROP/TRUNCATE). "
                "Confirme explicitamente para executar."
            ),
            "query": query,
        }

    userdata_url = _get_userdata_url()

    try:
        engine = create_engine(userdata_url, echo=False)
        with engine.connect() as conn:
            result = conn.execute(text(query), params or {})

            if result.returns_rows:
                columns = list(result.keys())
                rows = [dict(zip(columns, row)) for row in result.fetchall()]
                conn.commit()

                logger.info("sql.userdata_executed", read_only=True, row_count=len(rows))
                return {
                    "columns": columns,
                    "rows": rows,
                    "row_count": len(rows),
                }
            else:
                row_count = result.rowcount
                conn.commit()

                logger.info("sql.userdata_executed", read_only=False, row_count=row_count)
                return {
                    "message": f"Query executada. {row_count} linha(s) afetada(s).",
                    "row_count": row_count,
                }

    except SQLAlchemyError as e:
        logger.error("sql.userdata_error", query=query[:100], error=str(e))
        return {"error": "SQL_ERROR", "message": str(e)}
    finally:
        engine.dispose()


# ─── EXTERNAL — via vault ────────────────────────────────────

async def execute_external(
    connection_string: str,
    query: str,
    params: Optional[dict] = None,
    user_id: Optional[str] = None,
    confirm_destructive: bool = False,
) -> dict:
    """
    Execute query on an external database.
    Extra safety: blocks database-level operations entirely.
    """
    if _is_blocked_external(query):
        return {
            "error": "BLOCKED",
            "message": "❌ Operações de DROP/CREATE DATABASE não são permitidas em bancos externos.",
        }

    if not _is_read_only(query) and not confirm_destructive:
        return {
            "error": "CONFIRM_REQUIRED",
            "message": (
                "⚠️ Esta query modifica dados em um banco externo. "
                "Confirme explicitamente para executar."
            ),
            "query": query,
        }

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
                return {"columns": columns, "rows": rows, "row_count": len(rows)}
            else:
                row_count = result.rowcount
                conn.commit()
                return {"message": f"Query executada. {row_count} linha(s) afetada(s).", "row_count": row_count}

    except SQLAlchemyError as e:
        logger.error("sql.external_error", query=query[:100], error=str(e))
        return {"error": "SQL_ERROR", "message": str(e)}
    finally:
        engine.dispose()


# ─── FORMATTER ───────────────────────────────────────────────

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

    lines = [f"📊 *{total} resultado(s):*\n"]

    header = " | ".join(str(c) for c in columns)
    lines.append(f"`{header}`")
    lines.append(f"`{'─' * min(len(header), 60)}`")

    for row in rows[:max_rows]:
        values = " | ".join(str(row.get(c, ""))[:30] for c in columns)
        lines.append(f"`{values}`")

    if total > max_rows:
        lines.append(f"\n_... e mais {total - max_rows} linha(s)_")

    return "\n".join(lines)
