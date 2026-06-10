"""
Confluence RAG via Atlassian Rovo MCP — two-stage retrieval.

Stage A (pre-query):  search_business_context(question)
  Called BEFORE the orchestrator builds the OpenSearch query.
  Returns service topology / business rules from Confluence so the
  orchestrator understands which containers and fields to filter on.

Stage B (post-analysis): search_resolution_docs(error_codes, analysis_summary)
  Called AFTER the analyzer returns findings.
  Returns runbook steps and known workarounds for discovered BP error codes.

Both stages are no-ops when CONFLUENCE_ENABLED=false or API token is empty.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import config

logger = logging.getLogger(__name__)

# Lazy imports so the service starts normally even without the mcp package
_mcp_imported = False
_ClientSession = None
_StreamableHttpTransport = None


def _import_mcp() -> bool:
    global _mcp_imported, _ClientSession, _StreamableHttpTransport
    if _mcp_imported:
        return _ClientSession is not None
    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import StreamableHttpTransport

        _ClientSession = ClientSession
        _StreamableHttpTransport = StreamableHttpTransport
        _mcp_imported = True
        return True
    except ImportError:
        logger.warning(
            "mcp package not installed — Confluence RAG disabled. "
            "Run: pip install mcp>=1.0.0"
        )
        _mcp_imported = True  # don't retry
        return False


# ─── helpers ──────────────────────────────────────────────────────────────────

_MCP_URL = "https://mcp.atlassian.com/v1/mcp/authv2"


def _build_space_cql() -> str:
    """Return a CQL space filter clause if CONFLUENCE_SPACE_KEYS is configured."""
    keys = [k.strip() for k in config.CONFLUENCE_SPACE_KEYS.split(",") if k.strip()]
    if not keys:
        return ""
    quoted = ", ".join(f'"{k}"' for k in keys)
    return f" AND space IN ({quoted})"


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...[truncated]"


def _format_pages(pages: list[dict], max_chars: int) -> str:
    """Format a list of {title, body} dicts into a Markdown snippet."""
    parts: list[str] = []
    remaining = max_chars
    for page in pages:
        title = page.get("title", "Untitled")
        body = page.get("body", "").strip()
        if not body:
            continue
        snippet = _truncate(body, min(600, remaining))
        entry = f"**{title}**\n{snippet}\n---"
        parts.append(entry)
        remaining -= len(entry)
        if remaining <= 0:
            break
    return "\n".join(parts)


# ─── MCP tool caller ──────────────────────────────────────────────────────────

async def _call_mcp_tool(tool_name: str, arguments: dict[str, Any]) -> Any:
    """
    Open a fresh MCP session, call one tool, return the result content.
    Uses per-call sessions (no long-lived connection pool needed for low-concurrency use).
    """
    if not _import_mcp():
        return None

    auth_header = f"Bearer {config.CONFLUENCE_API_TOKEN}"
    try:
        transport = _StreamableHttpTransport(
            url=_MCP_URL,
            headers={"Authorization": auth_header},
        )
        async with _ClientSession(transport) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments)
            # Result is a list of content items; join text blocks
            if result and hasattr(result, "content"):
                texts = [
                    item.text
                    for item in result.content
                    if hasattr(item, "text") and item.text
                ]
                return "\n".join(texts) if texts else None
            return None
    except Exception as exc:
        logger.warning("MCP tool call %r failed: %s", tool_name, exc)
        return None


# ─── Page fetcher ─────────────────────────────────────────────────────────────

async def _fetch_page(page_id: str) -> str:
    """Fetch body text of a Confluence page by ID."""
    raw = await _call_mcp_tool("getConfluencePage", {"pageId": page_id})
    return raw or ""


async def _search_and_fetch(cql: str, top_k: int) -> list[dict]:
    """
    Run searchConfluenceUsingCql, then fetch full body for top_k results.
    Returns list of {title, body} dicts.
    """
    raw = await _call_mcp_tool(
        "searchConfluenceUsingCql",
        {"cql": cql, "limit": top_k},
    )
    if not raw:
        return []

    # Parse result — Atlassian MCP returns JSON text with results array
    import json as _json

    try:
        data = _json.loads(raw)
    except (_json.JSONDecodeError, TypeError):
        # raw may already be formatted text
        return [{"title": "Confluence result", "body": raw}]

    results = data if isinstance(data, list) else data.get("results", [])
    pages: list[dict] = []
    for item in results[:top_k]:
        title = (
            item.get("title")
            or item.get("content", {}).get("title", "Untitled")
        )
        page_id = (
            item.get("id")
            or item.get("content", {}).get("id")
        )
        body = ""
        if page_id:
            body = await _fetch_page(str(page_id))
        if not body:
            # Fall back to excerpt if full body unavailable
            body = (
                item.get("excerpt")
                or item.get("bodyText", "")
                or item.get("body", {}).get("view", {}).get("value", "")
                or ""
            )
        pages.append({"title": title, "body": body})
    return pages


# ─── Public API ───────────────────────────────────────────────────────────────

async def search_business_context(question: str) -> str:
    """
    Stage A — called BEFORE the orchestrator builds the OpenSearch query.

    Natural-language search for service topology and business logic docs
    relevant to the user's question.  Returns a Markdown snippet capped at
    CONFLUENCE_MAX_CHARS, or empty string if disabled / no results.
    """
    if not config.CONFLUENCE_ENABLED or not config.CONFLUENCE_API_TOKEN:
        return ""

    logger.info("[Confluence Stage A] Searching business context for: %r", question[:100])
    space_filter = _build_space_cql()

    pages: list[dict] = []

    # Primary: natural-language search via searchAtlassian (Rovo beta)
    raw_rovo = await _call_mcp_tool(
        "searchAtlassian",
        {"query": question, "limit": config.CONFLUENCE_TOP_K},
    )
    if raw_rovo:
        import json as _json

        try:
            data = _json.loads(raw_rovo)
            results = data if isinstance(data, list) else data.get("results", [])
            for item in results[: config.CONFLUENCE_TOP_K]:
                title = item.get("title", "Untitled")
                page_id = item.get("id")
                body = await _fetch_page(str(page_id)) if page_id else ""
                if not body:
                    body = item.get("excerpt", "")
                pages.append({"title": title, "body": body})
        except Exception:
            pages = [{"title": "Confluence (Rovo)", "body": raw_rovo}]

    # Fallback: CQL keyword search if Rovo returned nothing
    if not pages:
        # Extract key terms (avoid stop words)
        terms = re.findall(r"\b[A-Za-z0-9_\-]{4,}\b", question)[:6]
        if terms:
            term_cql = " OR ".join(f'text ~ "{t}"' for t in terms)
            cql = f"({term_cql}) AND type = page{space_filter} ORDER BY lastModified DESC"
            pages = await _search_and_fetch(cql, config.CONFLUENCE_TOP_K)

    if not pages:
        logger.info("[Confluence Stage A] No results found")
        return ""

    result = _format_pages(pages, config.CONFLUENCE_MAX_CHARS)
    logger.info("[Confluence Stage A] Returning %d chars from %d pages", len(result), len(pages))
    return result


async def search_resolution_docs(error_codes: list[str], analysis_summary: str) -> str:
    """
    Stage B — called AFTER the analyzer returns findings.

    Searches Confluence runbooks for the discovered BP error codes and returns
    resolution steps + known workarounds.  Returns a Markdown snippet capped at
    CONFLUENCE_MAX_CHARS, or empty string if disabled / no results.
    """
    if not config.CONFLUENCE_ENABLED or not config.CONFLUENCE_API_TOKEN:
        return ""

    space_filter = _build_space_cql()
    unique_codes = list(dict.fromkeys(error_codes))  # preserve order, deduplicate

    logger.info("[Confluence Stage B] Searching resolution docs for codes: %s", unique_codes)

    pages: list[dict] = []

    if unique_codes:
        # CQL: (text ~ "BP50002" OR text ~ "BP50006") AND type = page [AND space IN ...]
        code_terms = " OR ".join(f'text ~ "{c}"' for c in unique_codes[:6])
        cql = f"({code_terms}) AND type = page{space_filter} ORDER BY lastModified DESC"
        pages = await _search_and_fetch(cql, config.CONFLUENCE_TOP_K)

    # Fallback: search using the analysis summary keywords if no BP codes found
    if not pages and analysis_summary:
        terms = re.findall(r"\b[A-Za-z0-9_\-]{4,}\b", analysis_summary)[:6]
        if terms:
            term_cql = " OR ".join(f'text ~ "{t}"' for t in terms)
            cql = f"({term_cql}) AND type = page{space_filter} ORDER BY lastModified DESC"
            pages = await _search_and_fetch(cql, config.CONFLUENCE_TOP_K)

    if not pages:
        logger.info("[Confluence Stage B] No resolution docs found")
        return ""

    result = _format_pages(pages, config.CONFLUENCE_MAX_CHARS)
    logger.info("[Confluence Stage B] Returning %d chars from %d pages", len(result), len(pages))
    return result
