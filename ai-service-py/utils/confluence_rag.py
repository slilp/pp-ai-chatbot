"""
Confluence RAG — hybrid two-path retrieval.

Two complementary paths are used depending on task complexity:

  ┌─────────────────────────────────────────────────────────────────────────┐
  │  SIMPLE / CLEAR TARGET → Direct Confluence REST API                     │
  │  • Stage B (post-analysis): search by specific BP error codes           │
  │  • Any search where the target keyword is already known                 │
  │  • Fast, no extra dependencies                                          │
  ├─────────────────────────────────────────────────────────────────────────┤
  │  COMPLEX → Atlassian Rovo MCP (Teamwork Graph)                         │
  │  • Stage A (pre-query): understand service topology & relationships     │
  │  • Cross-product context: Confluence ↔ Jira ↔ Services                 │
  │  • Requires CONFLUENCE_MCP_TOKEN (Rovo MCP OAuth / API token with      │
  │    Teamwork Graph org-admin access)                                     │
  │  • Falls back to REST if MCP is unavailable or permission denied        │
  └─────────────────────────────────────────────────────────────────────────┘

Pipeline:

  User question
    → [Stage A - COMPLEX] Rovo MCP graph context (service topology)
              ↕ fallback if MCP unavailable
    → [Stage A - SIMPLE]  REST CQL full-text search
    → Orchestrator builds better OpenSearch query
    → Analyzer executes query + analyzes logs
    → [Stage B - SIMPLE]  REST CQL search for found BP error codes
    → Orchestrator synthesises Thai response with runbook steps

Required config (REST path):
  CONFLUENCE_ENABLED=true
  CONFLUENCE_SITE_URL=https://yourcompany.atlassian.net
  CONFLUENCE_USER_EMAIL=you@company.com
  CONFLUENCE_API_TOKEN=<classic-api-token>

Optional config (MCP path):
  CONFLUENCE_MCP_TOKEN=<rovo-mcp-token-with-teamwork-graph-access>
  CONFLUENCE_SPACE_KEYS=RUNBOOKS,OPS,PAYMENT  (empty = all spaces)
  CONFLUENCE_TOP_K=3
  CONFLUENCE_MAX_CHARS=1500
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import config

logger = logging.getLogger(__name__)

# ─── lazy MCP imports (optional path) ────────────────────────────────────────

_mcp_ready: bool | None = None   # None = not checked yet
_ClientSession = None
_streamablehttp_client = None

_MCP_URL = "https://mcp.atlassian.com/v1/mcp/authv2"


def _ensure_mcp() -> bool:
    """Import mcp package once; return True if available."""
    global _mcp_ready, _ClientSession, _streamablehttp_client
    if _mcp_ready is not None:
        return _mcp_ready
    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
        _ClientSession = ClientSession
        _streamablehttp_client = streamablehttp_client
        _mcp_ready = True
    except ImportError:
        logger.info("mcp package not installed — Rovo MCP path disabled")
        _mcp_ready = False
    return _mcp_ready


# ─── REST API helpers ─────────────────────────────────────────────────────────

def _basic_auth_header() -> str:
    creds = f"{config.CONFLUENCE_USER_EMAIL}:{config.CONFLUENCE_API_TOKEN}"
    return "Basic " + base64.b64encode(creds.encode()).decode()


def _is_rest_configured() -> bool:
    return bool(
        config.CONFLUENCE_ENABLED
        and config.CONFLUENCE_SITE_URL
        and config.CONFLUENCE_USER_EMAIL
        and config.CONFLUENCE_API_TOKEN
    )


def _is_mcp_configured() -> bool:
    return bool(
        config.CONFLUENCE_ENABLED
        and getattr(config, "CONFLUENCE_MCP_TOKEN", "")
    )


def _rest_get(path: str, params: dict[str, Any] | None = None) -> dict | None:
    """Synchronous GET against the Confluence REST API. Returns parsed JSON or None."""
    url = f"{config.CONFLUENCE_SITE_URL}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None}
        )
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": _basic_auth_header(),
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        logger.warning("Confluence REST %s → HTTP %d: %s", path, exc.code, exc.read()[:200])
    except Exception as exc:
        logger.warning("Confluence REST %s failed: %s", path, exc)
    return None


def _cql_search(cql: str, limit: int) -> list[dict]:
    """Run a CQL search; returns raw result items with body.view expanded."""
    data = _rest_get(
        "/wiki/rest/api/content/search",
        {"cql": cql, "limit": limit, "expand": "body.view,space"},
    )
    return (data or {}).get("results", [])


def _page_url(page: dict) -> str | None:
    """Build the full browser URL for a page result."""
    webui = page.get("_links", {}).get("webui", "")
    if webui:
        return f"{config.CONFLUENCE_SITE_URL}/wiki{webui}"
    page_id = page.get("id")
    return f"{config.CONFLUENCE_SITE_URL}/wiki/pages/{page_id}" if page_id else None


def _page_body_text(page: dict) -> str:
    """Strip HTML and inline CSS from body.view.value → plain text."""
    html = page.get("body", {}).get("view", {}).get("value", "")
    if not html:
        return page.get("excerpt", "").strip()
    # Remove <style> / <script> blocks (Confluence CSS/JS noise)
    html = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    # Strip remaining tags, collapse whitespace
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text


def _space_cql_filter() -> str:
    """Return a CQL space IN (...) clause if CONFLUENCE_SPACE_KEYS is set."""
    keys = [k.strip() for k in config.CONFLUENCE_SPACE_KEYS.split(",") if k.strip()]
    if not keys:
        return ""
    quoted = ", ".join(f'"{k}"' for k in keys)
    return f" AND space IN ({quoted})"


def _truncate(text: str, max_chars: int) -> str:
    return text if len(text) <= max_chars else text[:max_chars] + "\n...[truncated]"


def _format_rest_pages(pages: list[dict], max_chars: int) -> str:
    """Format REST API result list into a Markdown snippet."""
    parts: list[str] = []
    remaining = max_chars
    for page in pages:
        title = page.get("title", "Untitled")
        space_key = (page.get("space") or {}).get("key", "")
        body = _page_body_text(page)
        if not body:
            continue
        snippet = _truncate(body, min(600, remaining))
        space_tag = f" [{space_key}]" if space_key else ""
        entry = f"**{title}**{space_tag}\n{snippet}\n---"
        parts.append(entry)
        remaining -= len(entry)
        if remaining <= 0:
            break
    return "\n".join(parts)


# ─── MCP / Rovo Teamwork Graph path ──────────────────────────────────────────

async def _mcp_call(tool: str, args: dict[str, Any]) -> str | None:
    """
    Call one Rovo MCP tool.  Returns joined text content or None on any error.
    Uses CONFLUENCE_MCP_TOKEN (separate from the classic REST API token).
    """
    if not _ensure_mcp():
        return None
    mcp_token = getattr(config, "CONFLUENCE_MCP_TOKEN", "")
    if not mcp_token:
        return None

    try:
        async with _streamablehttp_client(
            url=_MCP_URL,
            headers={"Authorization": f"Bearer {mcp_token}"},
        ) as (read_stream, write_stream, _):
            async with _ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(tool, args)
                if result and hasattr(result, "content"):
                    texts = [
                        item.text
                        for item in result.content
                        if hasattr(item, "text") and item.text
                    ]
                    raw = "\n".join(texts)
                    # Treat permission errors as "no result" rather than crashing
                    if '"error":true' in raw or "don't have permission" in raw.lower():
                        logger.info(
                            "[Confluence MCP] Permission denied for %r — "
                            "needs Teamwork Graph org-admin access. Falling back to REST.",
                            tool,
                        )
                        return None
                    return raw or None
    except Exception as exc:
        logger.warning("[Confluence MCP] %r failed: %s", tool, exc)
    return None


async def _mcp_graph_context(question: str, space_keys: list[str]) -> str:
    """
    COMPLEX path — use Rovo MCP Teamwork Graph to retrieve cross-product
    context for up to 3 Confluence spaces relevant to the question.

    Returns Markdown snippet or empty string.
    """
    if not space_keys:
        return ""

    parts: list[str] = []
    site = config.CONFLUENCE_SITE_URL

    # For each space, get Teamwork Graph context (pages + linked Jira/services)
    tasks = [
        _mcp_call("getTeamworkGraphContext", {
            "cloudId": site,
            "objectType": "ConfluenceSpace",
            "objectIdentifier": key,
            "detailLevel": "summary",
            "first": 10,
        })
        for key in space_keys[:3]
    ]
    results = await asyncio.gather(*tasks)

    for key, raw in zip(space_keys[:3], results):
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            data = raw
        # Summarise the graph context as readable Markdown
        summary = _summarise_graph(data, key)
        if summary:
            parts.append(summary)

    return "\n\n".join(parts) if parts else ""


def _summarise_graph(data: Any, space_key: str) -> str:
    """Convert Teamwork Graph JSON into a brief Markdown summary."""
    if isinstance(data, str):
        return f"**Space [{space_key}] graph context:**\n{_truncate(data, 800)}\n---"

    if not isinstance(data, dict):
        return ""

    lines = [f"**Space [{space_key}] — related entities:**"]
    # Relationships list (schema varies by MCP version)
    relationships = (
        data.get("relationships")
        or data.get("edges")
        or data.get("connections")
        or []
    )
    if isinstance(relationships, list):
        for rel in relationships[:10]:
            rel_type = rel.get("type") or rel.get("relationshipType", "linked")
            target = rel.get("target") or rel.get("targetObject") or {}
            name = (
                target.get("title")
                or target.get("name")
                or target.get("key")
                or str(target)[:60]
            )
            lines.append(f"  - {rel_type}: {name}")
    else:
        lines.append(f"  {_truncate(str(data), 400)}")

    lines.append("---")
    return "\n".join(lines)


async def _mcp_fetch_pages(page_urls: list[str]) -> str:
    """
    COMPLEX path — use MCP getTeamworkGraphObject to fetch full content of
    pages found by the REST search.  Returns Markdown or empty string.
    """
    if not page_urls:
        return ""
    raw = await _mcp_call("getTeamworkGraphObject", {
        "cloudId": config.CONFLUENCE_SITE_URL,
        "objects": page_urls[:10],
    })
    if not raw:
        return ""
    # Raw is JSON; try to summarise it
    try:
        data = json.loads(raw)
        objects = data if isinstance(data, list) else data.get("objects", [data])
        parts: list[str] = []
        for obj in objects[:5]:
            title = obj.get("title") or obj.get("name", "Page")
            body = obj.get("body") or obj.get("content", "")
            if isinstance(body, dict):
                body = body.get("text") or body.get("value", "")
            snippet = _truncate(str(body), 400)
            parts.append(f"**{title}** (Graph)\n{snippet}\n---")
        return "\n".join(parts)
    except Exception:
        return _truncate(raw, 800)


# ─── Public API ───────────────────────────────────────────────────────────────

async def search_business_context(question: str) -> str:
    """
    Stage A — called BEFORE the orchestrator builds the OpenSearch query.

    Strategy (both paths run; results are merged):
    1. [COMPLEX] Rovo MCP Teamwork Graph — cross-product context for configured spaces.
       Shows which Jira projects / services / components link to each space.
       Requires CONFLUENCE_MCP_TOKEN with Teamwork Graph org-admin access.
    2. [SIMPLE]  Direct REST CQL search — pages whose text or title matches
       the question keywords. Works with any classic API token.

    Returns a Markdown snippet (≤ CONFLUENCE_MAX_CHARS) or empty string.
    """
    if not _is_rest_configured():
        return ""

    logger.info("[Confluence Stage A] question=%r", question[:80])
    space_filter = _space_cql_filter()

    # ── COMPLEX: MCP graph context (launched concurrently with REST search) ──
    space_keys = [k.strip() for k in config.CONFLUENCE_SPACE_KEYS.split(",") if k.strip()]

    # ── SIMPLE: REST CQL text search ──
    terms = list(dict.fromkeys(re.findall(r"\b[A-Za-z0-9_\-]{4,}\b", question)))[:8]
    rest_pages: list[dict] = []
    if terms:
        term_cql = " OR ".join(f'text ~ "{t}"' for t in terms)
        cql = f"({term_cql}) AND type = page{space_filter} ORDER BY lastModified DESC"
        rest_pages = _cql_search(cql, config.CONFLUENCE_TOP_K)

    # Title fallback if full-text returns nothing
    if not rest_pages and terms:
        title_cql = " OR ".join(f'title ~ "{t}"' for t in terms[:4])
        cql = f"({title_cql}) AND type = page{space_filter} ORDER BY lastModified DESC"
        rest_pages = _cql_search(cql, config.CONFLUENCE_TOP_K)

    # Await MCP graph context (only if configured)
    mcp_context = await _mcp_graph_context(question, space_keys) if _is_mcp_configured() else ""

    rest_context = _format_rest_pages(rest_pages, config.CONFLUENCE_MAX_CHARS)

    if not rest_context and not mcp_context:
        logger.info("[Confluence Stage A] No results from either path")
        return ""

    # MCP enrichment first (relationship context), then REST page content
    parts = [p for p in [mcp_context, rest_context] if p]
    result = _truncate("\n\n".join(parts), config.CONFLUENCE_MAX_CHARS)
    logger.info(
        "[Confluence Stage A] %d chars (MCP=%d, REST=%d pages)",
        len(result), len(mcp_context), len(rest_pages),
    )
    return result


async def search_resolution_docs(error_codes: list[str], analysis_summary: str) -> str:
    """
    Stage B — called AFTER the analyzer returns findings.

    Strategy (SIMPLE / clear target):
    • Direct REST CQL search for the specific BP error codes found in the analysis.
    • Target is explicit → direct search is faster and more precise than graph traversal.
    • No MCP call here: the error codes ARE the search keys.

    Returns a Markdown snippet (≤ CONFLUENCE_MAX_CHARS) or empty string.
    """
    if not _is_rest_configured():
        return ""

    space_filter = _space_cql_filter()
    unique_codes = list(dict.fromkeys(error_codes))
    logger.info("[Confluence Stage B] codes=%s", unique_codes)

    pages: list[dict] = []

    if unique_codes:
        code_terms = " OR ".join(f'text ~ "{c}"' for c in unique_codes[:6])
        cql = f"({code_terms}) AND type = page{space_filter} ORDER BY lastModified DESC"
        pages = _cql_search(cql, config.CONFLUENCE_TOP_K)

    # Fallback: keyword search from analysis summary
    if not pages and analysis_summary:
        terms = list(dict.fromkeys(re.findall(r"\b[A-Za-z0-9_\-]{4,}\b", analysis_summary)))[:6]
        if terms:
            term_cql = " OR ".join(f'text ~ "{t}"' for t in terms)
            cql = f"({term_cql}) AND type = page{space_filter} ORDER BY lastModified DESC"
            pages = _cql_search(cql, config.CONFLUENCE_TOP_K)

    if not pages:
        logger.info("[Confluence Stage B] No resolution docs found")
        return ""

    result = _format_rest_pages(pages, config.CONFLUENCE_MAX_CHARS)
    logger.info("[Confluence Stage B] %d chars from %d pages", len(result), len(pages))
    return result
