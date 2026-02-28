"""
SimpleClaw v3.0 - Tool Functions
==================================
Funções síncronas que as tools do agent loop chamam.
Compatível com event loop existente (Telegram async).
"""

from __future__ import annotations

import asyncio
import json
from typing import Optional

import structlog

logger = structlog.get_logger()


def _run_async(coro):
    """Run async coroutine from sync context, compatible with running event loop."""
    try:
        loop = asyncio.get_running_loop()
        # Already inside an event loop — use nest_asyncio or thread
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result(timeout=120)
    except RuntimeError:
        # No running loop — safe to use asyncio.run
        return asyncio.run(coro)


# ─── SEARCH ─────────────────────────────────────────────────

def search_web(query: str, max_results: int = 5) -> str:
    """
    Pesquisar na web via SearXNG.

    Args:
        query: Termo de busca
        max_results: Número máximo de resultados (padrão: 5)

    Returns:
        Resultados formatados da pesquisa
    """
    from src.tools.searxng_search import searxng_search, format_search_results

    try:
        results = _run_async(searxng_search(query, max_results=max_results))
        return format_search_results(results)
    except Exception as e:
        return f"Erro na pesquisa: {str(e)}"


# ─── SQL ─────────────────────────────────────────────────────

def run_sql(query: str, database: str = "userdata") -> str:
    """
    Executar query SQL no banco de dados.

    Args:
        query: Query SQL a ser executada
        database: 'userdata' para o banco de dados do usuário (padrão), 'system' para consultar o banco interno (somente leitura), ou nome da credencial no vault para bancos externos

    Returns:
        Resultado formatado da query
    """
    from src.tools.sql_executor import (
        execute_internal,
        execute_userdata,
        execute_external,
        format_query_result,
        _is_write,
    )

    try:
        if database == "system":
            # System DB: read-only for the model
            if _is_write(query):
                return "❌ O banco de sistema é somente leitura. Use database='userdata' para criar tabelas e inserir dados."
            result = _run_async(execute_internal(query))
        elif database in ("userdata", "internal"):
            # Userdata DB: full access
            result = _run_async(execute_userdata(query))
        else:
            # External DB: via vault
            result = _run_async(execute_external(database, query))
        return format_query_result(result)
    except Exception as e:
        return f"Erro SQL: {str(e)}"


# ─── FILE GENERATION ────────────────────────────────────────

def create_csv(data_json: str, filename: str = "dados.csv") -> str:
    """
    Gerar arquivo CSV.

    Args:
        data_json: JSON string com lista de dicts (cada dict = uma linha)
        filename: Nome do arquivo de saída

    Returns:
        Caminho do arquivo gerado
    """
    from src.tools.file_generator import generate_csv

    try:
        data = json.loads(data_json)
        path = generate_csv(data, filename)
        return f"✅ CSV gerado: {path}"
    except Exception as e:
        return f"Erro ao gerar CSV: {str(e)}"


def create_xlsx(data_json: str, filename: str = "dados.xlsx", sheet_name: str = "Dados") -> str:
    """
    Gerar arquivo Excel (XLSX).

    Args:
        data_json: JSON string com lista de dicts
        filename: Nome do arquivo
        sheet_name: Nome da aba

    Returns:
        Caminho do arquivo gerado
    """
    from src.tools.file_generator import generate_xlsx

    try:
        data = json.loads(data_json)
        path = generate_xlsx(data, filename, sheet_name)
        return f"✅ Excel gerado: {path}"
    except Exception as e:
        return f"Erro ao gerar Excel: {str(e)}"


def create_pdf(content: str, filename: str = "documento.pdf", title: str = "") -> str:
    """
    Gerar arquivo PDF.

    Args:
        content: Conteúdo de texto para o PDF
        filename: Nome do arquivo
        title: Título do documento

    Returns:
        Caminho do arquivo gerado
    """
    from src.tools.file_generator import generate_pdf

    try:
        path = generate_pdf(content, filename, title or None)
        return f"✅ PDF gerado: {path}"
    except Exception as e:
        return f"Erro ao gerar PDF: {str(e)}"


def create_docx(content: str, filename: str = "documento.docx", title: str = "") -> str:
    """
    Gerar arquivo Word (DOCX).

    Args:
        content: Conteúdo de texto para o documento
        filename: Nome do arquivo
        title: Título do documento

    Returns:
        Caminho do arquivo gerado
    """
    from src.tools.file_generator import generate_docx

    try:
        path = generate_docx(content, filename, title or None)
        return f"✅ DOCX gerado: {path}"
    except Exception as e:
        return f"Erro ao gerar DOCX: {str(e)}"


def create_chart(
    data_json: str,
    chart_type: str = "bar",
    filename: str = "grafico.png",
    title: str = "",
) -> str:
    """
    Gerar gráfico como imagem PNG.

    Args:
        data_json: JSON com 'labels' e 'values' (ex: {"labels": ["A","B"], "values": [10,20]})
        chart_type: Tipo do gráfico (bar, line, pie, scatter)
        filename: Nome do arquivo
        title: Título do gráfico

    Returns:
        Caminho do arquivo gerado
    """
    from src.tools.file_generator import generate_chart

    try:
        data = json.loads(data_json)
        path = generate_chart(data, chart_type, filename, title or None)
        return f"✅ Gráfico gerado: {path}"
    except Exception as e:
        return f"Erro ao gerar gráfico: {str(e)}"


# ─── CODE EXECUTION ─────────────────────────────────────────

def execute_python(code: str, task_id: str = "default", packages: str = "") -> str:
    """
    Executar código Python em ambiente isolado.

    Args:
        code: Código Python a ser executado
        task_id: Identificador da tarefa (para isolamento)
        packages: Pacotes necessários separados por vírgula (ex: "pandas,requests")

    Returns:
        Saída da execução (stdout + stderr + arquivos criados)
    """
    from src.tools.process_manager import ProcessManager

    try:
        pm = ProcessManager()

        # Install packages if needed
        if packages:
            pkg_list = [p.strip() for p in packages.split(",") if p.strip()]
            install_result = _run_async(pm.install_packages(task_id, pkg_list))
            if not install_result.get("success"):
                return f"Erro ao instalar pacotes: {install_result.get('stderr', install_result.get('error', ''))}"

        result = _run_async(pm.execute_code(task_id, code))

        output_parts = []
        if result.get("stdout"):
            output_parts.append(f"📤 Saída:\n{result['stdout']}")
        if result.get("stderr") and not result.get("success"):
            output_parts.append(f"⚠️ Erros:\n{result['stderr']}")
        if result.get("files_created"):
            output_parts.append(f"📁 Arquivos criados: {', '.join(result['files_created'])}")

        status = "✅" if result.get("success") else "❌"
        output_parts.insert(0, f"{status} Execução {'bem-sucedida' if result.get('success') else 'falhou'}")

        return "\n\n".join(output_parts)
    except Exception as e:
        return f"Erro na execução: {str(e)}"


# ─── GIT ─────────────────────────────────────────────────────

def git_save(message: str, task_dir: str = "") -> str:
    """
    Salvar checkpoint no git (commit do estado atual).

    Args:
        message: Mensagem descrevendo o que foi feito
        task_dir: Diretório do trabalho (opcional)

    Returns:
        Hash do commit ou mensagem de erro
    """
    from pathlib import Path
    from src.tools.git_checkpoint import GitCheckpoint

    try:
        gc = GitCheckpoint(Path(task_dir) if task_dir else None)
        commit_hash = gc.checkpoint(message)
        if commit_hash:
            return f"✅ Checkpoint salvo: {commit_hash[:8]} - {message}"
        return "⚠️ Nada para commitar."
    except Exception as e:
        return f"Erro no git: {str(e)}"


def git_history(max_entries: int = 10) -> str:
    """
    Ver histórico de checkpoints recentes.

    Args:
        max_entries: Número máximo de entradas

    Returns:
        Histórico formatado
    """
    from src.tools.git_checkpoint import GitCheckpoint

    try:
        gc = GitCheckpoint()
        entries = gc.get_log(max_entries)
        if not entries:
            return "Nenhum checkpoint encontrado."

        lines = ["📋 *Histórico de checkpoints:*\n"]
        for e in entries:
            lines.append(f"`{e['hash']}` {e['message']}")
        return "\n".join(lines)
    except Exception as e:
        return f"Erro ao ler histórico: {str(e)}"


# ─── TOOL SETS FOR AGENTS ───────────────────────────────────

def get_router_tools() -> list:
    """Tools available to the router agent."""
    return [search_web]


def get_db_architect_tools() -> list:
    """Tools for the DB Architect (Alexandre)."""
    from src.tools.superset_manager import (
        superset_query,
        superset_list_databases,
        superset_create_dataset,
    )
    return [run_sql, git_save, superset_query, superset_list_databases, superset_create_dataset]


def get_code_wizard_tools() -> list:
    """Tools for the Code Wizard (Marina)."""
    from src.tools.superset_manager import (
        superset_create_dashboard,
        superset_list_dashboards,
    )
    return [
        execute_python,
        create_csv,
        create_xlsx,
        create_pdf,
        create_docx,
        create_chart,
        git_save,
        superset_create_dashboard,
        superset_list_dashboards,
    ]


def get_devops_tools() -> list:
    """Tools for the DevOps Engineer (Carlos)."""
    return [execute_python, git_save, git_history]


def get_research_tools() -> list:
    """Tools for the Research Analyst (Sophia)."""
    return [search_web]
