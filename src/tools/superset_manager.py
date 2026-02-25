"""
SimpleClaw v2.0 - Apache Superset Manager Tool
=================================================
Manages Apache Superset via REST API.
Supports: datasets, charts, dashboards, SQL Lab queries,
database connections, and user management.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx
import structlog

from src.config.settings import get_settings

logger = structlog.get_logger()


class SupersetManager:
    """
    Manages Apache Superset instance via REST API.

    Capabilities:
    - Execute SQL queries via SQL Lab
    - Create/update/delete datasets
    - Create/update/delete charts
    - Create/update/delete dashboards
    - Manage database connections
    - Export/import dashboards
    - Manage users and roles
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8088",
        username: str = "admin",
        password: str = "admin",
    ):
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._access_token: Optional[str] = None
        self._csrf_token: Optional[str] = None
        self._client: Optional[httpx.AsyncClient] = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client with auth."""
        if self._client and self._access_token:
            return self._client

        self._client = httpx.AsyncClient(timeout=30)

        # Authenticate and get JWT token
        auth_response = await self._client.post(
            f"{self._base_url}/api/v1/security/login",
            json={
                "username": self._username,
                "password": self._password,
                "provider": "db",
            },
        )

        if auth_response.status_code != 200:
            raise ConnectionError(
                f"Superset auth failed ({auth_response.status_code}): "
                f"{auth_response.text[:200]}"
            )

        data = auth_response.json()
        self._access_token = data.get("access_token")

        # Get CSRF token
        csrf_response = await self._client.get(
            f"{self._base_url}/api/v1/security/csrf_token/",
            headers={"Authorization": f"Bearer {self._access_token}"},
        )
        if csrf_response.status_code == 200:
            self._csrf_token = csrf_response.json().get("result")

        logger.info("superset.authenticated", url=self._base_url)
        return self._client

    def _headers(self) -> dict:
        """Build request headers with auth and CSRF."""
        h = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }
        if self._csrf_token:
            h["X-CSRFToken"] = self._csrf_token
            h["Referer"] = self._base_url
        return h

    async def _request(
        self,
        method: str,
        endpoint: str,
        data: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> dict:
        """Make authenticated request to Superset API."""
        client = await self._ensure_client()
        url = f"{self._base_url}{endpoint}"

        response = await client.request(
            method,
            url,
            json=data,
            params=params,
            headers=self._headers(),
        )

        if response.status_code >= 400:
            error_msg = response.text[:300]
            logger.error(
                "superset.request_failed",
                method=method,
                endpoint=endpoint,
                status=response.status_code,
                error=error_msg,
            )
            return {"error": True, "status": response.status_code, "message": error_msg}

        try:
            return response.json()
        except Exception:
            return {"result": response.text}

    # ─── SQL LAB ────────────────────────────────────────────

    async def execute_sql(
        self,
        sql: str,
        database_id: int = 1,
        schema: Optional[str] = None,
        limit: int = 1000,
    ) -> dict:
        """
        Execute SQL query via Superset SQL Lab.

        Args:
            sql: SQL query to execute
            database_id: Superset database connection ID
            schema: Database schema
            limit: Max rows to return

        Returns:
            Dict with columns, data, and query info
        """
        payload = {
            "database_id": database_id,
            "sql": sql,
            "runAsync": False,
            "select_as_cta": False,
            "ctas_method": "TABLE",
            "queryLimit": limit,
        }
        if schema:
            payload["schema"] = schema

        result = await self._request("POST", "/api/v1/sqllab/execute/", data=payload)

        if result.get("error"):
            return result

        return {
            "columns": result.get("columns", []),
            "data": result.get("data", []),
            "query_id": result.get("query_id"),
            "rows_affected": result.get("rows", 0),
            "status": result.get("status", "unknown"),
        }

    # ─── DATABASES ──────────────────────────────────────────

    async def list_databases(self) -> list[dict]:
        """List all database connections in Superset."""
        result = await self._request("GET", "/api/v1/database/")
        databases = result.get("result", [])
        return [
            {
                "id": db.get("id"),
                "name": db.get("database_name"),
                "backend": db.get("backend"),
                "expose_in_sqllab": db.get("expose_in_sqllab"),
            }
            for db in databases
        ]

    async def add_database(
        self,
        name: str,
        sqlalchemy_uri: str,
        expose_in_sqllab: bool = True,
    ) -> dict:
        """Add a new database connection to Superset."""
        return await self._request(
            "POST",
            "/api/v1/database/",
            data={
                "database_name": name,
                "sqlalchemy_uri": sqlalchemy_uri,
                "expose_in_sqllab": expose_in_sqllab,
                "allow_ctas": True,
                "allow_cvas": True,
                "allow_dml": True,
                "allow_run_async": True,
            },
        )

    async def test_database_connection(self, sqlalchemy_uri: str) -> dict:
        """Test if a database connection string works."""
        return await self._request(
            "POST",
            "/api/v1/database/test_connection/",
            data={"sqlalchemy_uri": sqlalchemy_uri},
        )

    # ─── DATASETS ───────────────────────────────────────────

    async def list_datasets(self, page: int = 0, page_size: int = 25) -> list[dict]:
        """List datasets with pagination."""
        result = await self._request(
            "GET",
            "/api/v1/dataset/",
            params={"q": f"(page:{page},page_size:{page_size})"},
        )
        datasets = result.get("result", [])
        return [
            {
                "id": ds.get("id"),
                "table_name": ds.get("table_name"),
                "schema": ds.get("schema"),
                "database": ds.get("database", {}).get("database_name"),
                "kind": ds.get("kind"),
            }
            for ds in datasets
        ]

    async def create_dataset(
        self,
        table_name: str,
        database_id: int,
        schema: Optional[str] = None,
    ) -> dict:
        """Register an existing table as a Superset dataset."""
        payload = {
            "table_name": table_name,
            "database": database_id,
        }
        if schema:
            payload["schema"] = schema
        return await self._request("POST", "/api/v1/dataset/", data=payload)

    async def refresh_dataset(self, dataset_id: int) -> dict:
        """Refresh dataset metadata (columns, metrics)."""
        return await self._request("PUT", f"/api/v1/dataset/{dataset_id}/refresh")

    # ─── CHARTS ─────────────────────────────────────────────

    async def list_charts(self, page: int = 0, page_size: int = 25) -> list[dict]:
        """List charts."""
        result = await self._request(
            "GET",
            "/api/v1/chart/",
            params={"q": f"(page:{page},page_size:{page_size})"},
        )
        charts = result.get("result", [])
        return [
            {
                "id": c.get("id"),
                "name": c.get("slice_name"),
                "viz_type": c.get("viz_type"),
                "datasource": c.get("datasource_name_text"),
            }
            for c in charts
        ]

    async def create_chart(
        self,
        name: str,
        viz_type: str,
        datasource_id: int,
        datasource_type: str = "table",
        params: Optional[dict] = None,
    ) -> dict:
        """Create a new chart."""
        payload = {
            "slice_name": name,
            "viz_type": viz_type,
            "datasource_id": datasource_id,
            "datasource_type": datasource_type,
            "params": params or {},
        }
        return await self._request("POST", "/api/v1/chart/", data=payload)

    async def get_chart_data(self, chart_id: int) -> dict:
        """Get chart data/results."""
        return await self._request("GET", f"/api/v1/chart/{chart_id}/data/")

    # ─── DASHBOARDS ─────────────────────────────────────────

    async def list_dashboards(self) -> list[dict]:
        """List all dashboards."""
        result = await self._request("GET", "/api/v1/dashboard/")
        dashboards = result.get("result", [])
        return [
            {
                "id": d.get("id"),
                "title": d.get("dashboard_title"),
                "status": d.get("status"),
                "url": f"{self._base_url}/superset/dashboard/{d.get('id')}/",
                "published": d.get("published"),
            }
            for d in dashboards
        ]

    async def create_dashboard(
        self,
        title: str,
        chart_ids: Optional[list[int]] = None,
        published: bool = True,
    ) -> dict:
        """Create a new dashboard."""
        payload = {
            "dashboard_title": title,
            "published": published,
        }
        result = await self._request("POST", "/api/v1/dashboard/", data=payload)

        # Add charts to dashboard if provided
        if chart_ids and result.get("id"):
            dashboard_id = result["id"]
            # Build position JSON with charts
            position = {}
            for i, chart_id in enumerate(chart_ids):
                key = f"CHART-{chart_id}"
                position[key] = {
                    "type": "CHART",
                    "id": key,
                    "children": [],
                    "meta": {
                        "chartId": chart_id,
                        "width": 6,
                        "height": 50,
                    },
                }

            await self._request(
                "PUT",
                f"/api/v1/dashboard/{dashboard_id}",
                data={"position_json": str(position)},
            )

        return result

    async def export_dashboard(self, dashboard_id: int) -> dict:
        """Export a dashboard (returns export info)."""
        return await self._request("GET", f"/api/v1/dashboard/export/?q=[{dashboard_id}]")

    # ─── UTILITY ────────────────────────────────────────────

    async def get_health(self) -> dict:
        """Check Superset instance health."""
        try:
            client = await self._ensure_client()
            response = await client.get(f"{self._base_url}/health")
            return {"healthy": response.status_code == 200, "status": response.status_code}
        except Exception as e:
            return {"healthy": False, "error": str(e)}

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None


# ─── AGNO TOOL WRAPPERS ────────────────────────────────────

_superset: Optional[SupersetManager] = None


async def _get_superset() -> SupersetManager:
    """Get or create Superset manager from vault credentials."""
    global _superset
    if _superset:
        return _superset

    from src.tools.vault import Vault
    vault = Vault()

    url = await vault.retrieve("superset_url") or "http://localhost:8088"
    username = await vault.retrieve("superset_username") or "admin"
    password = await vault.retrieve("superset_password") or "admin"

    _superset = SupersetManager(url, username, password)
    return _superset


def superset_query(sql: str, database_id: int = 1) -> str:
    """
    Executar query SQL no Apache Superset via SQL Lab.

    Args:
        sql: Query SQL a executar
        database_id: ID da conexão de banco no Superset (padrão: 1)

    Returns:
        Resultado da query formatado
    """
    import asyncio

    async def _run():
        ss = await _get_superset()
        result = await ss.execute_sql(sql, database_id)
        if result.get("error"):
            return f"❌ Erro: {result.get('message', 'desconhecido')}"
        rows = result.get("data", [])
        cols = [c.get("name", c) if isinstance(c, dict) else c for c in result.get("columns", [])]
        if not rows:
            return "✅ Query executada. Nenhum resultado."
        lines = [f"📊 {len(rows)} resultado(s):\n"]
        header = " | ".join(str(c) for c in cols)
        lines.append(f"`{header}`")
        for row in rows[:20]:
            if isinstance(row, dict):
                vals = " | ".join(str(row.get(c, ""))[:25] for c in cols)
            else:
                vals = " | ".join(str(v)[:25] for v in (row if isinstance(row, (list, tuple)) else [row]))
            lines.append(f"`{vals}`")
        if len(rows) > 20:
            lines.append(f"\n_... e mais {len(rows) - 20} linhas_")
        return "\n".join(lines)

    try:
        return asyncio.run(_run())
    except Exception as e:
        return f"Erro Superset: {str(e)}"


def superset_list_dashboards() -> str:
    """
    Listar todos os dashboards do Apache Superset.

    Returns:
        Lista formatada de dashboards com ID, título e URL
    """
    import asyncio

    async def _run():
        ss = await _get_superset()
        dashboards = await ss.list_dashboards()
        if not dashboards:
            return "Nenhum dashboard encontrado."
        lines = ["📊 *Dashboards no Superset:*\n"]
        for d in dashboards:
            status = "🟢" if d.get("published") else "🔴"
            lines.append(f"{status} *{d['title']}* (ID: {d['id']})")
            lines.append(f"   🔗 {d.get('url', '')}")
        return "\n".join(lines)

    try:
        return asyncio.run(_run())
    except Exception as e:
        return f"Erro Superset: {str(e)}"


def superset_list_databases() -> str:
    """
    Listar conexões de banco de dados no Superset.

    Returns:
        Lista de databases configurados
    """
    import asyncio

    async def _run():
        ss = await _get_superset()
        databases = await ss.list_databases()
        if not databases:
            return "Nenhuma conexão de banco configurada."
        lines = ["🗄️ *Bancos de dados no Superset:*\n"]
        for db in databases:
            lines.append(f"• *{db['name']}* (ID: {db['id']}, backend: {db.get('backend', '?')})")
        return "\n".join(lines)

    try:
        return asyncio.run(_run())
    except Exception as e:
        return f"Erro Superset: {str(e)}"


def superset_create_dataset(table_name: str, database_id: int = 1, schema: str = "") -> str:
    """
    Registrar uma tabela existente como dataset no Superset.

    Args:
        table_name: Nome da tabela no banco
        database_id: ID da conexão de banco no Superset
        schema: Schema da tabela (opcional)

    Returns:
        Confirmação ou erro
    """
    import asyncio

    async def _run():
        ss = await _get_superset()
        result = await ss.create_dataset(table_name, database_id, schema or None)
        if result.get("error"):
            return f"❌ Erro: {result.get('message', 'desconhecido')}"
        ds_id = result.get("id", "?")
        return f"✅ Dataset '{table_name}' criado com ID {ds_id}"

    try:
        return asyncio.run(_run())
    except Exception as e:
        return f"Erro Superset: {str(e)}"


def superset_create_dashboard(title: str) -> str:
    """
    Criar novo dashboard no Apache Superset.

    Args:
        title: Título do dashboard

    Returns:
        Confirmação com ID e URL
    """
    import asyncio

    async def _run():
        ss = await _get_superset()
        result = await ss.create_dashboard(title)
        if result.get("error"):
            return f"❌ Erro: {result.get('message', 'desconhecido')}"
        dash_id = result.get("id", "?")
        return f"✅ Dashboard '{title}' criado (ID: {dash_id})\n🔗 {ss._base_url}/superset/dashboard/{dash_id}/"

    try:
        return asyncio.run(_run())
    except Exception as e:
        return f"Erro Superset: {str(e)}"
