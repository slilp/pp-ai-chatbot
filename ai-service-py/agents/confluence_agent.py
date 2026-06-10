"""
Confluence sub-agent — LLM-driven ReAct loop over Atlassian Rovo MCP tools.

Architecture mirrors analyzer.py:
  - Receives a task string (what context to find)
  - Runs a tool-calling loop: LLM decides which MCP tool to call, executes it, feeds result back
  - Iterates up to MAX_AGENT_ITERATIONS
  - Returns a plain Markdown string to the caller (orchestrator)

Used for COMPLEX retrieval (Stage A — pre-query):
  The orchestrator calls `run(task)` BEFORE building the OpenSearch query so it can
  inject Confluence business context into its system prompt.

Falls back to empty string on any error — caller always continues with REST fallback.

MCP tools the agent can call:
  - getTeamworkGraphContext   → cross-product relationships for a space / page / issue
  - getTeamworkGraphObject    → full content of pages/issues by URL or ARI
"""

import json
import logging
import re

from openai import OpenAI

import config
from utils.mcp_client import mcp_call, is_mcp_ready

logger = logging.getLogger(__name__)

# ─── load prompt once ─────────────────────────────────────────────────────────

_RAW_PROMPT: str = (config.PROMPTS_DIR / "system_confluence_agent.md").read_text()
MAX_AGENT_ITERATIONS: int = int(config.__dict__.get("CONFLUENCE_AGENT_MAX_ITER", 5))
_SYSTEM_PROMPT: str = _RAW_PROMPT.replace("{{MAX_ITERATIONS}}", str(MAX_AGENT_ITERATIONS))

# ─── LLM client (reuses orchestrator endpoint) ────────────────────────────────

_client = OpenAI(
    base_url=config.LLM_BASE_URL,
    api_key=config.LLM_API_KEY,
)

# ─── MCP tool definitions for the LLM ────────────────────────────────────────
# These mirror the actual Rovo MCP Teamwork Graph tool schemas so the LLM
# knows exactly what arguments to produce.

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "getTeamworkGraphContext",
            "description": (
                "Retrieve cross-product connected context from Teamwork Graph for a Confluence space, "
                "Confluence page, or Jira work item. Returns relationships and linked objects "
                "(linked Jira issues, child pages, related services, etc.). "
                "Use this to understand the topology around a service or space."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "objectType": {
                        "type": "string",
                        "enum": [
                            "ConfluenceSpace",
                            "ConfluencePage",
                            "ConfluenceBlogPost",
                            "JiraWorkItem",
                            "JiraSpace",
                        ],
                        "description": "Type of Atlassian object to get context for.",
                    },
                    "objectIdentifier": {
                        "type": "string",
                        "description": (
                            "Identifier for the object:\n"
                            "  - ConfluenceSpace: the space key, e.g. 'MAP', 'VFS', 'RUNBOOKS'\n"
                            "  - ConfluencePage: the full Confluence page URL or page ID\n"
                            "  - JiraWorkItem: Jira issue key, e.g. 'PP-123'\n"
                            "  - JiraSpace: Jira project key, e.g. 'PP'"
                        ),
                    },
                    "detailLevel": {
                        "type": "string",
                        "enum": ["summary", "full"],
                        "default": "summary",
                        "description": "'summary' for counts/metadata, 'full' for detailed content.",
                    },
                    "first": {
                        "type": "integer",
                        "default": 10,
                        "description": "Max number of related items to return (max 50).",
                    },
                },
                "required": ["objectType", "objectIdentifier"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "getTeamworkGraphObject",
            "description": (
                "Fetch full content of one or more Confluence pages or Jira issues by their "
                "full browser URL or ARI. Use after getTeamworkGraphContext reveals relevant "
                "page URLs worth reading in full."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "objects": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 5,
                        "description": (
                            "List of full browser URLs or ARIs, e.g.:\n"
                            "  ['https://company.atlassian.net/wiki/spaces/MAP/pages/123456']"
                        ),
                    },
                },
                "required": ["objects"],
            },
        },
    },
]


# ─── strip think tags ─────────────────────────────────────────────────────────

def _strip_think(s: str) -> str:
    return re.sub(r"<think>.*?</think>", "", s, flags=re.DOTALL).strip()


# ─── public API ───────────────────────────────────────────────────────────────

async def run(task: str, max_chars: int | None = None) -> str:
    """
    Run the Confluence sub-agent.

    Args:
        task:      Natural-language description of what context to retrieve.
                   Example: "Find service topology and BP50002 meaning for AIS payment"
        max_chars: Truncate the returned Markdown to this many characters.
                   Defaults to config.CONFLUENCE_MAX_CHARS.

    Returns:
        Markdown string with relevant context, or empty string on any failure.
    """
    if not is_mcp_ready():
        logger.debug("[Confluence Agent] MCP not configured — skipping")
        return ""

    limit = max_chars if max_chars is not None else config.CONFLUENCE_MAX_CHARS
    site  = config.CONFLUENCE_SITE_URL

    # Inject site URL into the task so the LLM can form correct identifiers
    task_with_ctx = f"Atlassian site: {site}\n\n{task}"

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user",   "content": f"/no_think\n{task_with_ctx}"},
    ]

    logger.info("[Confluence Agent] Starting — task=%r", task[:80])

    for iteration in range(MAX_AGENT_ITERATIONS):
        try:
            response = _client.chat.completions.create(
                model=config.ORCHESTRATOR_MODEL,
                max_tokens=2048,
                messages=messages,
                tools=_TOOLS,
                tool_choice="auto",
            )
        except Exception as exc:
            logger.warning("[Confluence Agent] LLM call failed: %s", exc)
            return ""

        choice    = response.choices[0]
        tool_calls = choice.message.tool_calls

        if not tool_calls:
            # Agent has finished — return its synthesised answer
            result = _strip_think(choice.message.content or "")
            if not result:
                result = _strip_think(getattr(choice.message, "reasoning", None) or "")
            logger.info(
                "[Confluence Agent] Done in %d iteration(s), %d chars",
                iteration + 1, len(result),
            )
            if result == "(no relevant documentation found)":
                return ""
            return result[:limit] if result else ""

        # ── execute tool calls ─────────────────────────────────────────────
        messages.append({
            "role": "assistant",
            "content": choice.message.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in tool_calls
            ],
        })

        for tc in tool_calls:
            tool_name = tc.function.name
            try:
                args = json.loads(_strip_think(tc.function.arguments))
            except (json.JSONDecodeError, ValueError):
                args = {}

            # Inject cloudId (required by all Teamwork Graph tools)
            args.setdefault("cloudId", site)

            logger.info(
                "[Confluence Agent] iter=%d tool=%r args=%s",
                iteration + 1,
                tool_name,
                json.dumps({k: v for k, v in args.items() if k != "cloudId"})[:120],
            )

            tool_result = await mcp_call(tool_name, args)

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": tool_result or "(tool returned no data)",
            })

    # Max iterations reached — ask the LLM to summarise what it has
    logger.warning(
        "[Confluence Agent] Max iterations (%d) reached; requesting summary",
        MAX_AGENT_ITERATIONS,
    )
    messages.append({
        "role": "user",
        "content": "/no_think\nBased on the information gathered so far, provide a concise summary.",
    })
    try:
        resp = _client.chat.completions.create(
            model=config.ORCHESTRATOR_MODEL,
            max_tokens=1024,
            messages=messages,
        )
        summary = _strip_think(resp.choices[0].message.content or "")
        return summary[:limit] if summary else ""
    except Exception as exc:
        logger.warning("[Confluence Agent] Summary call failed: %s", exc)
        return ""
