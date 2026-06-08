"""
Analyzer sub-agent.

Receives an OpenSearch query + analysis instructions, executes the query,
masks PII, then calls the LLM to produce a structured analysis.
Returns a plain-text analysis string to the orchestrator.

Isolation: raw log data never enters the orchestrator context window.
Uses OpenAI-compatible chat completions API.
"""

import json
import logging
import traceback
from datetime import datetime, timezone, timedelta

from openai import OpenAI

import config
from opensearch_client import client as os_client
from utils.pii_mask import mask_doc

logger = logging.getLogger(__name__)

_client = OpenAI(
    base_url=config.ANALYZER_BASE_URL,
    api_key=config.ANALYZER_API_KEY,
)

# Load prompts once at module import
_ANALYZER_PROMPT: str = (config.PROMPTS_DIR / "system_analyzer.md").read_text()
_BUSINESS_CTX: str = (config.PROMPTS_DIR / "business_context.md").read_text()
_SYSTEM_PROMPT = _ANALYZER_PROMPT.replace("{{BUSINESS_CONTEXT}}", _BUSINESS_CTX)

_BKK = timezone(timedelta(hours=7))


def _utc_to_bkk(ts: str) -> str:
    """Convert UTC ISO timestamp to Bangkok +07:00 for display."""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.astimezone(_BKK).strftime("%Y-%m-%d %H:%M:%S+07:00")
    except Exception:
        return ts


def _format_logs(docs: list[dict]) -> str:
    """Convert OpenSearch documents to compact text for the LLM."""
    if not docs:
        return "(no log entries found)"

    lines = [f"Total entries: {len(docs)}\n"]
    for i, doc in enumerate(docs, 1):
        ts = _utc_to_bkk(doc.get("@timestamp", ""))
        container = doc.get("container", "")
        namespace = doc.get("namespace", "")
        raw_msg = doc.get("message", "")

        # Parse the JSON message for compact display
        try:
            msg_parsed = json.loads(raw_msg)
            level = msg_parsed.get("level", "")
            log_type = msg_parsed.get("type", "")
            trace_id = msg_parsed.get("traceId", "")
            url = msg_parsed.get("url", "")
            status_code = msg_parsed.get("statusCode", "")
            time_ms = msg_parsed.get("timeInMS", "")
            error_code = msg_parsed.get("respErrorCode", "") or msg_parsed.get("errorCode", "")
            msg_text = msg_parsed.get("msg", "")
        except (json.JSONDecodeError, AttributeError):
            # Non-JSON message — include raw (truncated)
            lines.append(f"[{i}] {ts} | {container} ({namespace})")
            lines.append(f"    raw: {raw_msg[:200]}")
            continue

        header = f"[{i}] {ts} | {container} ({namespace}) | {level}"
        if log_type:
            header += f" | {log_type}"
        if trace_id:
            header += f" | traceId={trace_id}"

        details = []
        if url:
            details.append(f"url={url}")
        if status_code:
            details.append(f"status={status_code}")
        if time_ms:
            details.append(f"time={time_ms}ms")
        if error_code:
            details.append(f"errorCode={error_code}")

        lines.append(header)
        if details:
            lines.append("    " + " | ".join(details))
        if msg_text:
            truncated = msg_text if len(msg_text) <= 400 else msg_text[:400] + "...[truncated]"
            lines.append(f"    msg: {truncated}")
        lines.append("")

    return "\n".join(lines)


def analyze(
    opensearch_query: dict,
    analysis_focus: str,
    user_question: str,
) -> str:
    """
    Execute an OpenSearch query and return a structured analysis.

    Args:
        opensearch_query: Full OpenSearch Query DSL body.
        analysis_focus: What aspect to focus on.
        user_question: Original user question for context.

    Returns:
        Structured analysis text (markdown).
    """
    # Cap hits to avoid context overload
    if "size" not in opensearch_query:
        opensearch_query["size"] = config.MAX_LOG_HITS
    else:
        opensearch_query["size"] = min(opensearch_query["size"], config.MAX_LOG_HITS)

    logger.info("Analyzer executing query: %s", json.dumps(opensearch_query)[:300])

    try:
        docs = os_client.search(opensearch_query)
    except Exception as exc:
        logger.error("OpenSearch error: %s\n%s", exc, traceback.format_exc())
        return f"ERROR: Could not retrieve logs — {exc}"

    if not docs:
        return (
            "No log entries found matching the query. "
            "The time range, filters, or search term may not match any logs."
        )

    masked_docs = mask_doc(docs)
    log_text = _format_logs(masked_docs)

    # Hard cap: truncate log text to stay within the analyzer model's context window.
    if len(log_text) > config.MAX_LOG_CHARS:
        log_text = log_text[: config.MAX_LOG_CHARS]
        log_text += f"\n\n...[truncated to {config.MAX_LOG_CHARS} chars — increase MAX_LOG_CHARS or analyzer ctx-size to see more]"
        logger.warning(
            "Log text truncated to %d chars (had %d entries, %d chars total)",
            config.MAX_LOG_CHARS,
            len(docs),
            len(_format_logs(masked_docs)),
        )

    logger.info(
        "Analyzer sending %d entries to LLM (%d chars)",
        len(docs),
        len(log_text),
    )

    response = _client.chat.completions.create(
        model=config.ANALYZER_MODEL,
        max_tokens=2048,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"User question: {user_question}\n\n"
                    f"Analysis focus: {analysis_focus}\n\n"
                    f"Log entries:\n{log_text}"
                ),
            },
        ],
    )

    msg = response.choices[0].message
    analysis = msg.content or ""

    # Some models (GLM, Qwen) emit reasoning in a separate field; fall back to it
    if not analysis:
        reasoning = getattr(msg, "reasoning", None) or getattr(msg, "thinking", None)
        if reasoning:
            logger.info("Analyzer content empty, using reasoning field (%d chars)", len(reasoning))
            analysis = reasoning

    logger.info("Analyzer produced %d char analysis", len(analysis))
    return analysis
