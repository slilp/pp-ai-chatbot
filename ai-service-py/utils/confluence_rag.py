"""
Confluence RAG — two-stage retrieval via Confluence REST API.

Uses Basic auth (email + API token) against the Confluence Cloud REST API v1.
MCP/Rovo Graph tools are used as a supplementary enrichment layer when available,
but the primary search path is pure HTTP — no extra dependencies.

Stage A (pre-query):  search_business_context(question)
  Called BEFORE the orchestrator builds the OpenSearch query.
  Returns service topology / business rules from Confluence so the
  orchestrator understands which containers and fields to filter on.

Stage B (post-analysis): search_resolution_docs(error_codes, analysis_summary)
  Called AFTER the analyzer returns findings.
  Returns runbook steps and known workarounds for discovered BP error codes.

Both stages are no-ops when CONFLUENCE_ENABLED=false or credentials are missing.

Required config:
  CONFLUENCE_ENABLED=true
  CONFLUENCE_SITE_URL=https://yourcompany.atlassian.net
  CONFLUENCE_USER_EMAIL=you@company.com
  CONFLUENCE_API_TOKEN=<atlassian-api-token>

Optional:
  CONFLUENCE_SPACE_KEYS=RUNBOOKS,OPS,PAYMENT  (empty = all accessible spaces)
  CONFLUENCE_TOP_K=3
  CONFLUENCE_MAX_CHARS=1500
"""

from __future__ import annotations

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

# ─── auth helper ──────────────────────────────────────────────────────────────

def _basic_auth_header() -> str:
    creds = f"{config.CONFLUENCE_USER_EMAIL}:{config.CONFLUENCE_API_TOKEN}"
    return "Basic " + base64.b64encode(creds.encode()).decode()


def _is_configured() -> bool:
    return bool(
        config.CONFLUENCE_ENABLED
        and config.CONFLUENCE_SITE_URL
        and config.CONFLUENCE_USER_EMAIL
        and config.CONFLUENCE_API_TOKEN
    )


# ─── REST API helpers ─────────────────────────────────────────────────────────

def _api_get(path: str, params: dict[str, Any] | None = None) -> dict | None:
    """
    Synchronous GET against the Confluence REST API.
    Returns parsed JSON or None on error.
    """
    url = f"{config.CONFLUENCE_SITE_URL}{path}"
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": _basic_auth_header(),
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        logger.warning("Confluence API %s → HTTP %d: %s", path, exc.code, exc.read()[:200])
    except Exception as exc:
        logger.warning("Confluence API %s failed: %s", path, exc)
    return None


def _search_cql(cql: str, limit: int, expand: str = "body.view,space") -> list[dict]:
    """Run a CQL search and return raw result items."""
    data = _api_get(
        "/wiki/rest/api/content/search",
        {"cql": cql, "limit": limit, "expand": expand},
    )
    if not data:
        return []
    return data.get("results", [])


def _page_body(page: dict) -> str:
    """Extract plain-ish text from a page result (body.view.value)."""
    body_html = (
        page.get("body", {})
        .get("view", {})
        .get("value", "")
    )
    if not body_html:
        return ""
    # Remove <style>...</style> and <script>...</script> blocks (Confluence CSS/JS noise)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", body_html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    # Strip remaining HTML tags
    text = re.sub(r"<[^>]+>", " ", text)
    # Collapse whitespace
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text


# ─── helpers ──────────────────────────────────────────────────────────────────

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


def _format_results(pages: list[dict], max_chars: int) -> str:
    """Format a list of Confluence result dicts into a Markdown snippet."""
    parts: list[str] = []
    remaining = max_chars
    for page in pages:
        title = page.get("title", "Untitled")
        space_key = page.get("space", {}).get("key", "")
        body = _page_body(page)
        if not body:
            body = page.get("excerpt", "").strip()
        if not body:
            continue
        per_page_limit = min(600, remaining)
        snippet = _truncate(body, per_page_limit)
        space_tag = f" [{space_key}]" if space_key else ""
        entry = f"**{title}**{space_tag}\n{snippet}\n---"
        parts.append(entry)
        remaining -= len(entry)
        if remaining <= 0:
            break
    return "\n".join(parts)


# ─── Public API ───────────────────────────────────────────────────────────────

async def search_business_context(question: str) -> str:
    """
    Stage A — called BEFORE the orchestrator builds the OpenSearch query.

    Text search for service topology and business logic pages relevant to the
    user's question.  Returns a Markdown snippet (≤ CONFLUENCE_MAX_CHARS),
    or empty string if disabled / no results.
    """
    if not _is_configured():
        return ""

    logger.info("[Confluence Stage A] Searching business context for: %r", question[:100])
    space_filter = _build_space_cql()

    # Extract key terms from the question (skip short stop-words)
    terms = list(dict.fromkeys(re.findall(r"\b[A-Za-z0-9_\-]{4,}\b", question)))[:8]

    pages: list[dict] = []

    if terms:
        # Full-text CQL search across relevant terms
        term_cql = " OR ".join(f'text ~ "{t}"' for t in terms)
        cql = f"({term_cql}) AND type = page{space_filter} ORDER BY lastModified DESC"
        pages = _search_cql(cql, config.CONFLUENCE_TOP_K)

    # Fallback: plain title search if no full-text results
    if not pages and terms:
        title_cql = " OR ".join(f'title ~ "{t}"' for t in terms[:4])
        cql = f"({title_cql}) AND type = page{space_filter} ORDER BY lastModified DESC"
        pages = _search_cql(cql, config.CONFLUENCE_TOP_K)

    if not pages:
        logger.info("[Confluence Stage A] No results found")
        return ""

    result = _format_results(pages, config.CONFLUENCE_MAX_CHARS)
    logger.info("[Confluence Stage A] Returning %d chars from %d pages", len(result), len(pages))
    return result


async def search_resolution_docs(error_codes: list[str], analysis_summary: str) -> str:
    """
    Stage B — called AFTER the analyzer returns findings.

    Searches Confluence runbooks for the discovered BP error codes and returns
    resolution steps + known workarounds.  Returns a Markdown snippet
    (≤ CONFLUENCE_MAX_CHARS), or empty string if disabled / no results.
    """
    if not _is_configured():
        return ""

    space_filter = _build_space_cql()
    unique_codes = list(dict.fromkeys(error_codes))  # preserve order, deduplicate

    logger.info("[Confluence Stage B] Searching resolution docs for codes: %s", unique_codes)

    pages: list[dict] = []

    if unique_codes:
        # Exact BP error code search
        code_terms = " OR ".join(f'text ~ "{c}"' for c in unique_codes[:6])
        cql = f"({code_terms}) AND type = page{space_filter} ORDER BY lastModified DESC"
        pages = _search_cql(cql, config.CONFLUENCE_TOP_K)

    # Fallback: search using keywords from the analysis summary
    if not pages and analysis_summary:
        terms = list(dict.fromkeys(re.findall(r"\b[A-Za-z0-9_\-]{4,}\b", analysis_summary)))[:6]
        if terms:
            term_cql = " OR ".join(f'text ~ "{t}"' for t in terms)
            cql = f"({term_cql}) AND type = page{space_filter} ORDER BY lastModified DESC"
            pages = _search_cql(cql, config.CONFLUENCE_TOP_K)

    if not pages:
        logger.info("[Confluence Stage B] No resolution docs found")
        return ""

    result = _format_results(pages, config.CONFLUENCE_MAX_CHARS)
    logger.info("[Confluence Stage B] Returning %d chars from %d pages", len(result), len(pages))
    return result
