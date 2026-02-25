"""
SimpleClaw v2.0 - SearXNG Search Tool
=======================================
Web search via local SearXNG instance.
Zero API cost, privacy-first.
"""

from __future__ import annotations

import json
from typing import Optional

import httpx
import structlog

from src.config.settings import get_settings

logger = structlog.get_logger()


async def searxng_search(
    query: str,
    categories: str = "general",
    language: str = "pt-BR",
    max_results: int = 5,
    time_range: Optional[str] = None,
) -> list[dict]:
    """
    Search the web via local SearXNG instance.

    Args:
        query: Search query
        categories: Search categories (general, images, news, science, files)
        language: Language code
        max_results: Maximum results to return
        time_range: Time filter (day, week, month, year)

    Returns:
        List of search results with title, url, content
    """
    settings = get_settings()
    base_url = settings.searxng_url.rstrip("/")

    params = {
        "q": query,
        "format": "json",
        "categories": categories,
        "language": language,
        "pageno": 1,
    }
    if time_range:
        params["time_range"] = time_range

    try:
        async with httpx.AsyncClient(timeout=settings.searxng_timeout_seconds) as client:
            response = await client.get(f"{base_url}/search", params=params)
            response.raise_for_status()
            data = response.json()

        results = []
        for item in data.get("results", [])[:max_results]:
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "content": item.get("content", ""),
                "engine": item.get("engine", ""),
                "score": item.get("score", 0),
            })

        logger.info(
            "search.completed",
            query=query,
            results=len(results),
        )
        return results

    except httpx.ConnectError:
        logger.error("search.connection_failed", url=base_url)
        return [{"error": "SearXNG não está acessível. Verifique se o serviço está rodando."}]
    except Exception as e:
        logger.error("search.failed", query=query, error=str(e))
        return [{"error": f"Erro na busca: {str(e)}"}]


def format_search_results(results: list[dict]) -> str:
    """Format search results for display in chat."""
    if not results:
        return "Nenhum resultado encontrado."

    if results and "error" in results[0]:
        return f"⚠️ {results[0]['error']}"

    lines = ["🔍 *Resultados da pesquisa:*\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"*{i}. {r['title']}*")
        if r.get("content"):
            content = r["content"][:200]
            if len(r["content"]) > 200:
                content += "..."
            lines.append(f"   {content}")
        lines.append(f"   🔗 {r['url']}\n")

    return "\n".join(lines)
