"""
Log deduplication via MinHash LSH (P2).

Problem: a single error storm (e.g. 150 identical BP50002 errors) blows the
analyzer LLM's context window.  Sending 150 near-identical entries also
degrades analysis quality — the LLM drowns in noise instead of seeing signal.

Solution: cluster near-duplicate log entries by content similarity using
MinHash LSH, keep one representative per cluster, annotate it with a count,
and cap the output list at LOG_DEDUP_MAX_LOGS.

Result: 150 identical BP50002 errors → 1 entry + "[149 duplicates collapsed]"

Why Python-only: datasketch MinHash LSH is a pure-Python probabilistic data
structure with no production-grade equivalent in Go or Node.js.

Config:
  LOG_DEDUP_ENABLED            true / false  (default: true)
  LOG_DEDUP_SIMILARITY_THRESHOLD  0.0–1.0   (default: 0.85)
  LOG_DEDUP_MAX_LOGS           int           (default: 60)
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import config

logger = logging.getLogger(__name__)

# ─── lazy import ─────────────────────────────────────────────────────────────

_datasketch_ok: bool | None = None


def _ensure_datasketch() -> bool:
    global _datasketch_ok
    if _datasketch_ok is not None:
        return _datasketch_ok
    try:
        import datasketch  # noqa: F401
        _datasketch_ok = True
    except ImportError:
        logger.warning(
            "datasketch not installed — log deduplication disabled. "
            "Run: pip install datasketch>=1.6.0"
        )
        _datasketch_ok = False
    return _datasketch_ok


# ─── canonical fingerprint ────────────────────────────────────────────────────

# Fields that are unique per request and should be ignored when comparing
_HIGH_CARDINALITY = {"@timestamp", "traceId", "spanId", "reqId", "requestId", "timestamp"}


def _fingerprint(doc: dict) -> str:
    """
    Build a stable, low-cardinality string fingerprint for a log document.

    Strips high-cardinality fields (@timestamp, traceId, etc.) so that two
    entries that differ only in timestamp / traceId are considered duplicates.
    """
    raw_msg = doc.get("message", "")
    container = doc.get("container", "")
    namespace = doc.get("namespace", "")

    # Try to parse the JSON message for structured field extraction
    try:
        msg = json.loads(raw_msg)
        # Keep stable structural fields; drop per-request identifiers
        parts: dict[str, Any] = {}
        for k, v in msg.items():
            if k in _HIGH_CARDINALITY:
                continue
            if k == "msg" and isinstance(v, str):
                # Normalise numeric values inside messages (e.g. "took 123ms" → "took Nms")
                v = re.sub(r"\b\d+\b", "N", v)
            parts[k] = v
        fingerprint_str = json.dumps(parts, sort_keys=True, ensure_ascii=False)
    except (json.JSONDecodeError, AttributeError):
        # Non-JSON: normalise digits and use raw message
        fingerprint_str = re.sub(r"\b\d+\b", "N", raw_msg[:500])

    return f"{container}|{namespace}|{fingerprint_str}"


# ─── MinHash helpers ──────────────────────────────────────────────────────────

def _minhash(text: str, num_perm: int = 64):
    """Create a MinHash for a text string using character 4-grams."""
    from datasketch import MinHash  # type: ignore
    m = MinHash(num_perm=num_perm)
    # Shingling: character 4-grams provide good sensitivity for log text
    for i in range(max(1, len(text) - 3)):
        m.update(text[i: i + 4].encode("utf-8"))
    return m


# ─── public API ───────────────────────────────────────────────────────────────

def deduplicate_logs(docs: list[dict]) -> list[dict]:
    """
    Cluster near-duplicate log entries and collapse each cluster into one
    representative with an annotation.

    Args:
        docs: List of OpenSearch hit _source dicts (already PII-masked).

    Returns:
        Deduplicated list capped at LOG_DEDUP_MAX_LOGS entries.
        Each cluster representative may have an extra key
        ``_dedup_count`` (int ≥ 1) used by the formatter.
    """
    if not config.LOG_DEDUP_ENABLED:
        return docs

    if len(docs) <= 1:
        return docs

    if not _ensure_datasketch():
        # Graceful degradation: skip dedup, rely on MAX_LOG_CHARS truncation
        return docs

    from datasketch import MinHashLSH  # type: ignore

    threshold = config.LOG_DEDUP_SIMILARITY_THRESHOLD
    num_perm  = 64

    logger.info("Deduplicating %d log entries (threshold=%.2f)", len(docs), threshold)

    lsh = MinHashLSH(threshold=threshold, num_perm=num_perm)

    # cluster_id → index of the representative doc in `docs`
    clusters: dict[str, int] = {}
    # representative index → count of duplicates absorbed
    counts: dict[int, int] = {}
    # result: list of representative doc indices (in arrival order)
    representatives: list[int] = []

    for idx, doc in enumerate(docs):
        fp  = _fingerprint(doc)
        mh  = _minhash(fp, num_perm)
        key = f"doc_{idx}"

        neighbours = lsh.query(mh)

        if neighbours:
            # Find the earliest representative in this cluster
            rep_key  = min(neighbours, key=lambda k: int(k.split("_")[1]))
            rep_idx  = int(rep_key.split("_")[1])
            counts[rep_idx] = counts.get(rep_idx, 1) + 1
        else:
            # New cluster — this doc becomes the representative
            lsh.insert(key, mh)
            clusters[key] = idx
            counts[idx]   = 1
            representatives.append(idx)

    # Build output: annotate each representative with its duplicate count
    deduped: list[dict] = []
    for rep_idx in representatives:
        doc   = dict(docs[rep_idx])  # shallow copy so we don't mutate the original
        count = counts.get(rep_idx, 1)
        doc["_dedup_count"] = count
        deduped.append(doc)

    # Cap at max logs
    max_logs = config.LOG_DEDUP_MAX_LOGS
    if len(deduped) > max_logs:
        logger.info(
            "Dedup cap: keeping %d of %d unique clusters (dropped %d rarest)",
            max_logs, len(deduped), len(deduped) - max_logs,
        )
        deduped = deduped[:max_logs]

    total_original = len(docs)
    total_kept     = sum(d.get("_dedup_count", 1) for d in deduped)
    logger.info(
        "Dedup result: %d → %d entries (%.0f%% reduction, %d/%d docs represented)",
        total_original,
        len(deduped),
        100 * (1 - len(deduped) / total_original),
        total_kept,
        total_original,
    )
    return deduped
