"""
Confluence RAG — hybrid two-path retrieval.

  ┌──────────────────────────────────────────────────────────────────────────┐
  │ COMPLEX → confluence_agent (LLM subagent, Rovo MCP Teamwork Graph)       │
  │   Stage A only.  The LLM autonomously decides which MCP tools to call,   │
  │   iterates, and returns a synthesised Markdown context block.            │
  │   Requires CONFLUENCE_MCP_TOKEN + CONFLUENCE_GRAPH_TOKEN.               │
  │   Falls back to REST silently on permission denied / MCP unavailable.   │
  ├──────────────────────────────────────────────────────────────────────────┤
  │ SIMPLE / CLEAR TARGET → Direct Confluence REST API                       │
  │   Stage A: full-text CQL search using question keywords.                │
  │   Stage B: CQL search for specific BP error codes (known targets).      │
  │   Works with any classic Atlassian API token.                           │
  └──────────────────────────────────────────────────────────────────────────┘

Both stage A paths run concurrently; results are merged.
Stage B always uses REST (target is explicit — no graph traversal needed).

Required config (REST):
  CONFLUENCE_ENABLED=true
  CONFLUENCE_SITE_URL=https://yourcompany.atlassian.net
  CONFLUENCE_USER_EMAIL=you@company.com
  CONFLUENCE_API_TOKEN=<classic-api-token>

Optional config (MCP subagent):
  CONFLUENCE_MCP_TOKEN=<rovo-mcp-token>    # Bearer auth for MCP server
  CONFLUENCE_GRAPH_TOKEN=<graph-token>     # Bearer auth for Teamwork Graph tools
  CONFLUENCE_SPACE_KEYS=MAP,VFS,RUNBOOKS   # scope; empty = all spaces

⚠ Teamwork Graph needs org-admin policy:
  admin.atlassian.com → Security → API token policies → Teamwork Graph
"""

from __future__ import annotations

import base64
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request

import config
from utils.mcp_client import is_mcp_ready

logger = logging.getLogger(__name__)


# ─── REST auth helpers ────────────────────────────────────────────────────────

def _basic_auth() -> str:
    creds = f"{config.CONFLUENCE_USER_EMAIL}:{config.CONFLUENCE_API_TOKEN}"
    return "Basic " + base64.b64encode(creds.encode()).decode()


def _is_rest_ready() -> bool:
    return bool(
        config.CONFLUENCE_ENABLED
        and config.CONFLUENCE_SITE_URL
        and config.CONFLUENCE_USER_EMAIL
        and config.CONFLUENCE_API_TOKEN
    )


# ─── REST API ─────────────────────────────────────────────────────────────────

def _rest_get(path: str, params: dict | None = None) -> dict | None:
    """Synchronous GET against the Confluence REST API. Returns parsed JSON or None."""
    url = f"{config.CONFLUENCE_SITE_URL}{path}"
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(
        url,
        headers={"Authorization": _basic_auth(), "Accept": "application/json"},
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
    data = _rest_get(
        "/wiki/rest/api/content/search",
        {"cql": cql, "limit": limit, "expand": "body.view,space"},
    )
    return (data or {}).get("results", [])


def _page_body_text(page: dict) -> str:
    """Strip HTML / CSS from body.view.value → plain text."""
    html = page.get("body", {}).get("view", {}).get("value", "")
    if not html:
        return page.get("excerpt", "").strip()
    html = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s{2,}", " ", text).strip()


def _space_cql_filter() -> str:
    keys = [k.strip() for k in config.CONFLUENCE_SPACE_KEYS.split(",") if k.strip()]
    if not keys:
        return ""
    return " AND space IN (" + ", ".join(f'"{k}"' for k in keys) + ")"


def _truncate(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + "\n...[truncated]"


def _format_rest_pages(pages: list[dict], max_chars: int) -> str:
    parts: list[str] = []
    remaining = max_chars
    for page in pages:
        title     = page.get("title", "Untitled")
        space_key = (page.get("space") or {}).get("key", "")
        body      = _page_body_text(page)
        if not body:
            continue
        snippet   = _truncate(body, min(600, remaining))
        tag       = f" [{space_key}]" if space_key else ""
        entry     = f"**{title}**{tag}\n{snippet}\n---"
        parts.append(entry)
        remaining -= len(entry)
        if remaining <= 0:
            break
    return "\n".join(parts)


# ─── Public API ───────────────────────────────────────────────────────────────

async def search_business_context(question: str) -> str:
    """
    Stage A — called BEFORE the orchestrator builds the OpenSearch query.

    COMPLEX path  (run in parallel with REST):
      Delegates to confluence_agent.run() — the LLM subagent autonomously
      calls Rovo MCP Teamwork Graph tools to find cross-product context.

    SIMPLE path:
      Direct Confluence REST CQL search using question keywords.

    Returns merged Markdown (≤ CONFLUENCE_MAX_CHARS) or empty string.
    """
    if not _is_rest_ready():
        return ""

    logger.info("[Confluence Stage A] question=%r", question[:80])
    space_filter = _space_cql_filter()

    # ── COMPLEX: MCP subagent (import here to avoid circular import at module load)
    mcp_task = None
    if is_mcp_ready():
        from agents.confluence_agent import run as agent_run
        space_keys = [k.strip() for k in config.CONFLUENCE_SPACE_KEYS.split(",") if k.strip()]
        agent_task_text = (
            f"Find business context and service topology relevant to this question:\n"
            f"'{question}'\n\n"
            f"Focus on:\n"
            f"- Which Confluence spaces / pages describe the relevant payment service or flow\n"
            f"- What BP error codes mean in this context\n"
            f"- Which containers or microservices are involved\n"
        )
        if space_keys:
            agent_task_text += f"\nSearch in these Confluence spaces: {', '.join(space_keys)}"
        import asyncio
        mcp_task = asyncio.ensure_future(agent_run(agent_task_text))

    # ── SIMPLE: REST CQL text search ──────────────────────────────────────────
    terms = list(dict.fromkeys(re.findall(r"\b[A-Za-z0-9_\-]{4,}\b", question)))[:8]
    rest_pages: list[dict] = []
    if terms:
        cql = "(" + " OR ".join(f'text ~ "{t}"' for t in terms) + f") AND type = page{space_filter} ORDER BY lastModified DESC"
        rest_pages = _cql_search(cql, config.CONFLUENCE_TOP_K)
    if not rest_pages and terms:
        cql = "(" + " OR ".join(f'title ~ "{t}"' for t in terms[:4]) + f") AND type = page{space_filter} ORDER BY lastModified DESC"
        rest_pages = _cql_search(cql, config.CONFLUENCE_TOP_K)

    # ── Await MCP subagent (if running) ───────────────────────────────────────
    mcp_context = ""
    if mcp_task is not None:
        import asyncio
        try:
            mcp_context = await asyncio.wait_for(mcp_task, timeout=20.0) or ""
        except asyncio.TimeoutError:
            logger.warning("[Confluence Stage A] MCP subagent timed out (20s); using REST only")
            mcp_task.cancel()

    rest_context = _format_rest_pages(rest_pages, config.CONFLUENCE_MAX_CHARS)

    if not mcp_context and not rest_context:
        logger.info("[Confluence Stage A] No results from either path")
        return ""

    # MCP subagent result first (structured analysis), then REST snippets
    parts = [p for p in [mcp_context, rest_context] if p]
    result = _truncate("\n\n".join(parts), config.CONFLUENCE_MAX_CHARS)
    logger.info(
        "[Confluence Stage A] %d chars (subagent=%d chars, REST=%d pages)",
        len(result), len(mcp_context), len(rest_pages),
    )
    return result


async def search_resolution_docs(error_codes: list[str], analysis_summary: str) -> str:
    """
    Stage B — called AFTER the analyzer returns findings.

    SIMPLE / clear target — direct REST CQL:
    The BP error codes found in the analysis ARE the search keys,
    so direct CQL is faster and more precise than graph traversal.
    No MCP subagent is used here.

    Returns Markdown (≤ CONFLUENCE_MAX_CHARS) or empty string.
    """
    if not _is_rest_ready():
        return ""

    space_filter = _space_cql_filter()
    unique_codes = list(dict.fromkeys(error_codes))
    logger.info("[Confluence Stage B] codes=%s", unique_codes)

    pages: list[dict] = []

    if unique_codes:
        cql = "(" + " OR ".join(f'text ~ "{c}"' for c in unique_codes[:6]) + f") AND type = page{space_filter} ORDER BY lastModified DESC"
        pages = _cql_search(cql, config.CONFLUENCE_TOP_K)

    if not pages and analysis_summary:
        terms = list(dict.fromkeys(re.findall(r"\b[A-Za-z0-9_\-]{4,}\b", analysis_summary)))[:6]
        if terms:
            cql = "(" + " OR ".join(f'text ~ "{t}"' for t in terms) + f") AND type = page{space_filter} ORDER BY lastModified DESC"
            pages = _cql_search(cql, config.CONFLUENCE_TOP_K)

    if not pages:
        logger.info("[Confluence Stage B] No resolution docs found")
        return ""

    result = _format_rest_pages(pages, config.CONFLUENCE_MAX_CHARS)
    logger.info("[Confluence Stage B] %d chars from %d pages", len(result), len(pages))
    return result
