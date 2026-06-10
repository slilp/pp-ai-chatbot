"""
Orchestrator (main agent).

Flow:
  1. [Stage A] Fetch Confluence business context (if enabled) — service topology & rules.
  2. Build messages with system prompt + optional Confluence context.
  3. Non-streaming call with function definitions → LLM builds query + calls analyze_logs.
  4. Execute analyze_logs (sub-agent in analyzer.py).
  5. [Stage B] Fetch Confluence resolution docs for discovered BP error codes (if enabled).
  6. Feed tool result (+ resolution docs) back and stream the final Thai-language response.

Uses OpenAI-compatible chat completions API (function calling / tool_use).
"""

import json
import logging
import re
import traceback
from typing import AsyncIterator

from openai import OpenAI

import config
from agents.analyzer import analyze
from utils.confluence_rag import search_business_context, search_resolution_docs
from utils.sse import text_event, status_event, error_event, DONE_EVENT
from utils.think_filter import ThinkFilter

logger = logging.getLogger(__name__)

_client = OpenAI(
    base_url=config.LLM_BASE_URL,
    api_key=config.LLM_API_KEY,
)

# Load + build system prompt once
_ORCHESTRATOR_PROMPT: str = (config.PROMPTS_DIR / "system_orchestrator.md").read_text()
_BUSINESS_CTX: str = (config.PROMPTS_DIR / "business_context.md").read_text()

_SYSTEM_PROMPT = (
    _ORCHESTRATOR_PROMPT
    .replace("{{BUSINESS_CONTEXT}}", _BUSINESS_CTX)
    .replace("{{INDEX}}", config.OPENSEARCH_INDEX)
)

# OpenAI-format tool definition
_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "analyze_logs",
            "description": (
                "Execute an OpenSearch query against the payment logs index (payment-platform-uat-sample) "
                "and analyze the matching log entries. "
                "Call this to answer ANY question about payment logs, errors, transactions, or traces. "
                "IMPORTANT: all log content is inside the `message` text field — use match_phrase. "
                "Example query for errors in a time range:\n"
                '{"query":{"bool":{"filter":[{"match_phrase":{"message":"\\"level\\":\\"error\\""}},{"range":{"@timestamp":{"gte":"2026-06-08T07:00:00Z","lte":"2026-06-08T07:30:00Z"}}}]}},"sort":[{"@timestamp":"asc"}],"size":100}'
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "opensearch_query": {
                        "type": "object",
                        "description": (
                            "Full OpenSearch Query DSL body. Must include 'query', "
                            "'sort' ([{\"@timestamp\":\"asc\"}]), and 'size' (max 200). "
                            "Use match_phrase on 'message' field for traceId, error codes, keywords. "
                            "Use term on 'container' or 'namespace' for exact service filter. "
                            "Bangkok time = UTC+7, so subtract 7h for @timestamp filters."
                        ),
                    },
                    "analysis_focus": {
                        "type": "string",
                        "description": (
                            "What to focus on in the analysis. Examples: "
                            "'Find all BP error codes and their root causes', "
                            "'Reconstruct the full transaction flow step by step'."
                        ),
                    },
                },
                "required": ["opensearch_query", "analysis_focus"],
            },
        },
    }
]

_MAX_HISTORY = 10

# Today's date injected into the user message so the model knows the current date
import datetime as _dt
def _today_bkk() -> str:
    bkk = _dt.timezone(_dt.timedelta(hours=7))
    return _dt.datetime.now(bkk).strftime("%Y-%m-%d")


def _build_messages(
    question: str,
    history: list[dict],
    wiki_context: str = "",
) -> list[dict]:
    system_content = _SYSTEM_PROMPT
    if wiki_context:
        system_content += (
            "\n\n---\n## Relevant Documentation from Confluence\n"
            + wiki_context
            + "\n---"
        )
    messages = [{"role": "system", "content": system_content}]
    for msg in history[-_MAX_HISTORY:]:
        if msg.get("role") in ("user", "assistant"):
            messages.append({"role": msg["role"], "content": msg["content"]})
    # Prepend /no_think and today's date so time-relative queries work correctly
    today = _today_bkk()
    messages.append({
        "role": "user",
        "content": f"/no_think\n[Today Bangkok date: {today}]\n{question}",
    })
    return messages


def _strip_think_tags(s: str) -> str:
    """Remove <think>...</think> blocks and trailing whitespace from a string."""
    s = re.sub(r"<think>.*?</think>", "", s, flags=re.DOTALL)
    return s.strip()


def _validate_query(q: object) -> dict:
    """
    Ensure the query is a usable dict. Returns a broad fallback query if not.
    Corrects common model mistakes like wrong field names in match_phrase.
    """
    # Some models (GLM) wrap the JSON object as a string — try to parse it
    if isinstance(q, str):
        q = _strip_think_tags(q)
        try:
            q = json.loads(q)
            logger.info("Parsed string-encoded opensearch_query successfully")
        except json.JSONDecodeError:
            pass

    if not isinstance(q, dict):
        logger.warning("LLM returned non-dict query (%s), using fallback", type(q).__name__)
        return {
            "query": {"match_phrase": {"message": '"level":"error"'}},
            "sort": [{"@timestamp": "asc"}],
            "size": 50,
        }

    # Strip non-OpenSearch keys leaked into the top-level body (e.g. analysis_focus)
    _OS_KEYS = {"query", "sort", "size", "from", "aggs", "aggregations", "_source", "highlight", "track_total_hits"}
    for key in list(q.keys()):
        if key not in _OS_KEYS:
            logger.warning("Removing invalid opensearch_query key: %r", key)
            del q[key]

    # Rescue sort/size if model placed them inside the query object instead of top level
    inner = q.get("query")
    if isinstance(inner, dict):
        for misplaced in ("sort", "size"):
            if misplaced in inner:
                val = inner.pop(misplaced)
                if misplaced not in q:
                    logger.warning("Rescued %r from inside query object to top level", misplaced)
                    q[misplaced] = val

    # Ensure sort and size are present
    if "sort" not in q:
        q["sort"] = [{"@timestamp": "asc"}]
    if "size" not in q:
        q["size"] = 100

    # Fix common mistake: model uses "match_phrase_all" instead of "match_phrase"
    raw = json.dumps(q)
    raw = raw.replace('"match_phrase_all"', '"match_phrase"')

    # Fix missing quotes inside match_phrase values for level filter
    # e.g. {"message": "level:error"} → {"message": "\"level\":\"error\""}
    raw = re.sub(
        r'"message"\s*:\s*"level:error"',
        '"message": "\\"level\\":\\"error\\""',
        raw,
    )

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return q


async def orchestrate(
    question: str,
    history: list[dict],
) -> AsyncIterator[str]:
    """
    Async generator that yields SSE event strings.
    Emits status events during processing and text events for the final response.
    Think-tags from reasoning models are stripped from streamed output.
    """
    yield status_event("Understanding your question...")

    # Stage A: fetch Confluence business context before building the query.
    # This grounds the LLM in service topology and business rules so it builds
    # a more accurate OpenSearch query (correct containers, field names, etc.).
    wiki_context = await search_business_context(question)
    if wiki_context:
        logger.info("[Confluence Stage A] Injecting %d chars of wiki context", len(wiki_context))

    messages = _build_messages(question, history, wiki_context=wiki_context)

    # Step 1: Let the orchestrator decide — call analyze_logs or answer directly.
    # tool_choice="auto": model uses the tool for log queries, skips it for conversational follow-ups.
    # max_tokens=16384: reasoning models emit a <think> block before the tool call JSON.
    try:
        first_response = _client.chat.completions.create(
            model=config.ORCHESTRATOR_MODEL,
            max_tokens=16384,
            messages=messages,
            tools=_TOOLS,
            tool_choice="auto",
        )
    except Exception as exc:
        logger.error("Orchestrator first call failed: %s", exc)
        yield error_event(f"LLM error: {exc}")
        return

    choice = first_response.choices[0]
    logger.info("Orchestrator first call finish_reason: %s", choice.finish_reason)

    tool_calls = choice.message.tool_calls
    if not tool_calls:
        # Model chose to answer directly from conversation context — stream that response
        logger.info("No tool call — streaming direct answer (finish_reason=%s)", choice.finish_reason)
        direct_content = choice.message.content or ""
        if not direct_content:
            # Some reasoning models put output in reasoning field when content is empty
            direct_content = getattr(choice.message, "reasoning", None) or ""
        if not direct_content:
            yield error_event("LLM returned an empty response. Please rephrase your question.")
            return

        yield status_event("กำลังตอบ...")

        # Filter think tags from direct response before streaming
        tokens: list[str] = []
        think_filter = ThinkFilter(lambda t: tokens.append(t))
        think_filter.write(direct_content)
        think_filter.write("")  # flush
        for token in tokens:
            yield text_event(token)
        yield DONE_EVENT
        return

    # Step 2: Parse and validate the tool call
    tool_call = tool_calls[0]
    tool_call_id = tool_call.id

    logger.info("Raw tool arguments (%d chars): %s", len(tool_call.function.arguments), tool_call.function.arguments[:600])

    try:
        raw_args = json.loads(tool_call.function.arguments)
    except json.JSONDecodeError as exc:
        logger.error("Bad tool arguments JSON: %s | raw: %s", exc, tool_call.function.arguments[:300])
        yield error_event(f"Could not parse tool arguments: {exc}")
        return

    raw_query = raw_args.get("opensearch_query")
    # Strip think tags that GLM sometimes leaks into argument values
    if isinstance(raw_query, str):
        raw_query = _strip_think_tags(raw_query)
    logger.info("opensearch_query type=%s value=%s", type(raw_query).__name__, json.dumps(raw_query, default=str)[:300])

    opensearch_query = _validate_query(raw_query)
    analysis_focus: str = _strip_think_tags(raw_args.get("analysis_focus", "General log analysis"))

    logger.info(
        "Calling analyze_logs: focus=%r query=%s",
        analysis_focus,
        json.dumps(opensearch_query)[:300],
    )

    yield status_event("Searching payment logs...")

    # Step 3: Execute the sub-agent (analyzer)
    try:
        analysis_result = analyze(opensearch_query, analysis_focus, question)
    except Exception as exc:
        logger.error("Analyzer failed: %s\n%s", exc, traceback.format_exc())
        yield error_event(f"Analysis error: {exc}")
        return

    yield status_event("Summarizing results...")

    # Stage B: fetch Confluence resolution docs for discovered BP error codes.
    # Appended to the tool result so the final Thai response can cite runbook steps.
    bp_codes = re.findall(r"BP\d+", analysis_result)
    resolution_docs = await search_resolution_docs(bp_codes, analysis_result)
    if resolution_docs:
        logger.info(
            "[Confluence Stage B] Injecting %d chars of resolution docs (codes: %s)",
            len(resolution_docs),
            bp_codes,
        )

    # Build tool result content — append resolution docs if available
    tool_result_content = analysis_result
    if resolution_docs:
        tool_result_content += (
            "\n\n---\n## Relevant Runbook / Documentation from Confluence\n"
            + resolution_docs
        )

    # Step 4: Feed tool result back and stream the final Thai-language response
    messages.append({
        "role": "assistant",
        "content": choice.message.content or "",
        "tool_calls": [
            {
                "id": tool_call_id,
                "type": "function",
                "function": {
                    "name": tool_call.function.name,
                    "arguments": tool_call.function.arguments,
                },
            }
        ],
    })
    messages.append({
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": tool_result_content,
    })
    # Append instruction to respond in Thai without further tool calls
    messages.append({
        "role": "user",
        "content": "/no_think\nตอบเป็นภาษาไทย อธิบายผลการวิเคราะห์อย่างชัดเจน",
    })

    # Stream with think-tag filtering
    tokens: list[str] = []

    def _emit(token: str) -> None:
        tokens.append(token)

    think_filter = ThinkFilter(_emit)

    try:
        stream = _client.chat.completions.create(
            model=config.ORCHESTRATOR_MODEL,
            max_tokens=4096,
            messages=messages,
            stream=True,
        )
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta and delta.content:
                think_filter.write(delta.content)
                # Yield any tokens the filter released
                while tokens:
                    yield text_event(tokens.pop(0))
    except Exception as exc:
        logger.error("Orchestrator stream failed: %s", exc)
        yield error_event(f"LLM stream error: {exc}")
        return

    # Flush any remaining buffer
    think_filter.write("")
    while tokens:
        yield text_event(tokens.pop(0))

    yield DONE_EVENT
